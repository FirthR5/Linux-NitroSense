import os
import shutil
import struct

from PyQt6 import QtWidgets
from PyQt6.QtCore import QObject, QProcess, pyqtSignal


class CommandRunner(QObject):
    finished = pyqtSignal(str)  # Emits the full output when done

    def __init__(self):
        super().__init__()
        self.process = QProcess()
        self.process.readyReadStandardOutput.connect(self._handle_output)
        self.process.finished.connect(self._on_finished)
        self.output = ""  # Store output here

    def run(self, cmd, args):
        self.output = ""  # Reset output
        self.process.start(cmd, args)
        self.process.waitForFinished()

    def _handle_output(self):
        new_output = self.process.readAllStandardOutput().data().decode()
        self.output += new_output  # Append to stored output

    def _on_finished(self):
        self.finished.emit(self.output)  # Emit the full output

    def close(self):
        self.process.close()


class IntelMSR:
    MSR_OC_POWER = 0x150
    MSR_PERF_STATUS = 0x198

    PLANE_CORE = 0
    PLANE_CACHE = 2
    APPLIED_PLANES = (PLANE_CORE, PLANE_CACHE)
    PLANE_NAMES = {
        PLANE_CORE: "CPU Core",
        PLANE_CACHE: "CPU Cache",
    }

    # Offsets past this are gated behind a confirmation popup
    SAFE_OFFSET_LIMIT_MV = -200

    # IA32_PERF_STATUS voltage field steps by 1/8192 V
    VOLTAGE_SCALE = 8192.0

    def ensure_module(self):
        if not self._cpus():
            runner = CommandRunner()
            runner.run("modprobe", ["msr"])

    def _cpus(self):
        try:
            entries = os.listdir("/dev/cpu")
        except FileNotFoundError:
            return []
        return sorted(int(entry) for entry in entries if entry.isdigit())

    def _cores(self):
        # Voltage is shared per core, so one logical cpu per core is enough
        representatives = {}
        for cpu in self._cpus():
            path = f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
            try:
                with open(path) as f:
                    key = f.read().strip()
            except OSError:
                key = str(cpu)
            representatives.setdefault(key, cpu)
        return sorted(representatives.values())

    def _read(self, addr, cpu=0, quiet=False):
        path = f"/dev/cpu/{cpu}/msr"
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as e:
            if not quiet:
                print(f"Error: cannot open {path}: {e.strerror}")
            return None

        try:
            os.lseek(fd, addr, os.SEEK_SET)
            data = os.read(fd, 8)
        except OSError as e:
            if not quiet:
                print(f"Error: cannot read MSR {addr:#05x}: {e.strerror}")
            return None
        finally:
            os.close(fd)

        if len(data) != 8:
            if not quiet:
                print(f"Error: short read on MSR {addr:#05x}")
            return None
        return struct.unpack("Q", data)[0]

    def _write_all(self, addr, value):
        # Hyperthreaded siblings keep separate register state, so every logical
        # cpu has to be written for an offset to take effect across the package
        cpus = self._cpus()
        if not cpus:
            print("Error: no msr nodes found, run 'modprobe msr'")
            return False

        failures = []
        for cpu in cpus:
            path = f"/dev/cpu/{cpu}/msr"
            try:
                fd = os.open(path, os.O_WRONLY)
            except OSError as e:
                print(f"Error: cannot open {path}: {e.strerror}")
                return False

            try:
                os.lseek(fd, addr, os.SEEK_SET)
                os.write(fd, struct.pack("Q", value))
            except OSError as e:
                failures.append(f"cpu{cpu}: {e.strerror}")
            finally:
                os.close(fd)

        if failures:
            print(
                f"Error: the BIOS rejected the write to MSR {addr:#05x} "
                f"({'; '.join(failures)})"
            )
            return False
        return True

    def _convert(self, mv):
        # Offset is an 11 bit field where one step is 1/1024 V
        return 0xFFE00000 & ((int(round(mv * 1.024)) & 0xFFF) << 21)

    def _unconvert(self, field):
        value = field >> 21
        rounded = value if value <= 1024 else -(2048 - value)
        return rounded / 1.024

    def _unpack(self, response):
        plane = response >> 40
        return self._unconvert(response ^ (plane << 40))

    def get_offset(self, plane):
        # The mailbox only answers after being handed a read request
        if not self._write_all(self.MSR_OC_POWER, self._pack(plane)):
            return None

        response = self._read(self.MSR_OC_POWER, self._cpus()[0])
        if response is None:
            return None
        return self._unpack(response)

    def _pack(self, plane, offset=None):
        # With no offset this becomes a read request for the given plane
        return (
            (1 << 63)
            | (plane << 40)
            | (1 << 36)
            | ((offset is not None) << 32)
            | (offset or 0)
        )

    def set_offset(self, plane, mv):
        if mv > 0:
            print("Error: positive offsets are not allowed")
            return None

        if not self._write_all(
            self.MSR_OC_POWER, self._pack(plane, self._convert(mv))
        ):
            return None

        # A locked mailbox keeps the previous value instead of raising,
        # result has to be read back instead of assumed
        return self.get_offset(plane)

    def get_voltage(self):
        readings = []
        for cpu in self._cores():
            raw = self._read(self.MSR_PERF_STATUS, cpu, quiet=True)
            if raw is None:
                continue
            value = (raw >> 32) & 0xFFFF
            if value:
                readings.append(value)

        if not readings:
            return None
        return (sum(readings) / len(readings)) / self.VOLTAGE_SCALE

    def status_text(self):
        lines = []
        for plane in self.APPLIED_PLANES:
            offset = self.get_offset(plane)
            if offset is None:
                return (
                    "Cannot read the voltage offset. Root is required and the "
                    "msr module must be loaded."
                )
            lines.append(f"{self.PLANE_NAMES[plane]}\t{offset:+.0f}mV")
        return "\n".join(lines)


def _confirm(offset_mv):
    if offset_mv >= IntelMSR.SAFE_OFFSET_LIMIT_MV:
        return True

    answer = QtWidgets.QMessageBox.warning(
        None,
        "Aggressive undervolt",
        f"{offset_mv}mV is past the offset most 8th gen mobile parts handle "
        "reliably.\n\nIf the machine freezes or reboots, the offset is reset "
        "automatically on the next boot.",
        QtWidgets.QMessageBox.StandardButton.Ok
        | QtWidgets.QMessageBox.StandardButton.Cancel,
        QtWidgets.QMessageBox.StandardButton.Cancel,
    )
    return answer == QtWidgets.QMessageBox.StandardButton.Ok


def checkUndervoltStatus(self):
    msr = IntelMSR()
    msr.ensure_module()
    self.undervolt = msr.status_text()


def applyUndervolt(self):
    mv = self.undervolt_dropdown.currentIndex() * -100

    if not _confirm(mv):
        checkUndervoltStatus(self)
        return

    msr = IntelMSR()
    applied = []
    rejected = []
    for plane in IntelMSR.APPLIED_PLANES:
        result = msr.set_offset(plane, mv)
        if result is None:
            self.undervolt = (
                "Cannot write the voltage offset. Root is required and the BIOS "
                "needs Undervolt Protection and CF Lock disabled."
            )
            return

        name = IntelMSR.PLANE_NAMES[plane]
        if abs(result - mv) > 1:
            rejected.append(f"{name} wanted {mv}mV, got {result:+.0f}mV")
        else:
            applied.append(f"{name} {result:+.0f}mV")

    checkUndervoltStatus(self)

    if rejected:
        self.undervolt += "\n\nRejected: " + "; ".join(rejected)
    else:
        self.undervolt += "\n\nApplied: " + ", ".join(applied)


voltage_process = QProcess()


def checkVoltage(self):
    ## https://askubuntu.com/questions/876286/how-to-monitor-the-vcore-voltage
    voltage = None

    if shutil.which("rdmsr"):
        voltage_process.start("sudo rdmsr 0x198 -a -u --bitfield 47:32")  # All processors
        voltage_process.waitForFinished()
        output = voltage_process.readAll()

        if output:
            data = [int(line) for line in output.data().decode("utf-8").splitlines()]
            # print(data)
            avg_v = sum(data) / len(data)
            voltage = int(avg_v) / 8192

    if not voltage:
        voltage = IntelMSR().get_voltage()

    if not voltage:
        return

    self.voltage = voltage

    if voltage < self.minrecordedVoltage:
        self.minrecordedVoltage = voltage
    if voltage > self.maxrecordedVoltage:
        self.maxrecordedVoltage = voltage
