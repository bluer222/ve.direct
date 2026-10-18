> This project is primarily AI-generated code.

# ve.direct

An async Python library for Victron VE.Direct devices. Read Text telemetry,
read and write HEX registers, or inspect a device with the included local web GUI.
It supports MPPT, MPPT RS, Orion XS, BMV/SmartShunt, and Phoenix inverter families.
Requires Python 3.9 or newer.

## Usage

From a checkout, install the library and serial support, then log your device:

```sh
git clone https://github.com/bluer222/ve.direct.git
cd ve.direct
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ".[serial]"
python examples/async_log.py /dev/ttyUSB0
```

For a browser view, run `python tools/vedirect_gui.py --port /dev/ttyUSB0 --flavor MPPT --open`.
Use `--sim --open` instead of `--port /dev/ttyUSB0 --flavor MPPT` to try the GUI without hardware.

## Documentation

- [Usage guide](docs/USAGE.md): Python examples, device families, register writes, GUI, and connection recovery.
- [Text fields](docs/TEXT_FRAMES.md) and [HEX registers](docs/HEX_REGISTERS.md): protocol references.
- [Conformance scope](conformance/SCOPE.md) and [results](conformance/RESULTS.md): checks and limits.
- [Sources](docs/SOURCES.md), [specification exceptions](docs/SPEC_EXCEPTIONS.md), and [MPPT audit](docs/MPPT_DUMP_AUDIT.md).

First-party code is licensed under the GNU Affero General Public License v3.0 only
[AGPL-3.0-only](LICENCE). Bundled Victron specification excerpts retain their original notices.
