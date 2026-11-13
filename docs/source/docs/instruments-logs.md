Instruments and logs
====================

Sailworks can read live instrument data from NMEA 2000 gateways and record it for later
review. Instrument input is receive-only: the app does not send vessel-control or
transmit messages.

(recording-nmea-2000-instruments)=
Recording NMEA 2000 instruments
-------------------------------

The app reads complete NMEA 2000 PGNs from gateways that provide Actisense BST or
Actisense N2K ASCII output. The NGT-1 uses BST; an NGX-1 must be in transfer mode, and
its hardware compatibility has not yet been verified. N2K ASCII gateways must reassemble
fast and ISO transport packets before sending them to the app. These connections are
input only: a serial Actisense BST gateway receives one gateway-local receive-mode
command at connection time, but the app does not send vessel-control or NMEA 2000
transmit messages. Network data servers such as the W2K-1 are transmit only, so the app
never writes to them.

1. Connect the gateway to the powered NMEA 2000 network and your computer.
2. In **Settings > Connections**, set **Protocol** to **NMEA 2000**, then choose the
   **Gateway data format** your gateway provides. For an Actisense NGT-1, choose
   **Actisense BST**. For a gateway configured to output **N2K ASCII**, choose that
   format; it can use a serial port or a TCP/UDP network connection. For a W2K-1, set
   the data server **Format** to **Actisense NGT** (the app labels this **Actisense
   BST**), **Direction** to **Transmit Only**, and enter the server's IP address and
   port.
3. For a serial gateway, select its serial port (on macOS, normally
   `/dev/cu.usbserial-...`). If your operating system does not expose the port, install
   the appropriate Actisense USB driver. The Actisense BST connection defaults to
   **230400** baud; use **115200** for an older/configured interface. Set the gateway's
   output format to N2K ASCII before selecting that connection type.
4. Save the connection and check that instrument readings and its packet count update.
   In **Logs**, click **Record**, then **Stop** and **Save** after sailing.

Saving `sail.nmea` also saves `sail.nmea.n2k.log` when NMEA 2000 messages have been
received. The first file contains converted NMEA 0183 sentences for the existing Logs
display. The companion contains validated original PGN payloads in CANboat raw text
format, with UTC receipt timestamps, priorities, source/destination addresses and PGN
numbers. Unknown/proprietary PGNs are retained there too. The companion is a complete
session archive: cropping in Logs affects only the converted NMEA file. Save each sail
before starting another recording or using Clear; the working recording is temporary and
a new session replaces it.

Supported display conversions include heading (127250), speed through water (128259),
depth (128267), GPS position (129025/129029), COG/SOG (129026), wind (130306) and sea
temperature (130310/130312/130316). Geographic wind directions and ground-referenced
true wind are kept in the original archive rather than converted into water-referenced
wind angles. Position sentences use the Mac/PC UTC receipt time; synchronize its clock
before sailing. Original GNSS/system clock data remain available in the PGN archive.

The Logs speed graph uses GPS speed over ground. Its existing true-wind estimation is a
review aid, not a calibrated polar calculation. Use the original wind and water-speed
PGNs for performance analysis. The converted file is not yet a verified Polarize import;
raw-file conversion and source selection need validation against an actual recording.
The display consumes supported measurements from all received sources, so duplicate
sensors may alternate; the raw archive preserves source addresses for later selection.

### Try it without a boat

On macOS or Linux, run this from the source checkout in a separate terminal:

```sh
python3 tools/simulate_actisense.py
```

The simulator prints a pseudo-terminal path, such as `/dev/ttys012`. Enter that path in
the connection dialog's serial-port field, choose **NMEA 2000** and **Actisense BST**,
set 230400 baud, and leave the simulator running. It sends synthetic sailing data near
Vancouver: GPS position, heading, wind, water speed, depth and temperature. You can watch
the dashboard and make/save a Logs recording through the same binary input path used by
the real interface. Stop with Ctrl-C, then remove the simulated connection before
connecting the boat. `--duration 60` runs a one-minute simulation. The simulator needs
only Python's standard library; Windows pseudo-terminals are not supported.

(shared-nmea-streaming-and-playback)=
Shared NMEA streaming and playback
----------------------------------

Open **NMEA** from the toolbar in **Charts**, **Logs**, or **Polars**. All three tabs
share one sidebar and one NMEA component. Open components stay visible when switching
tabs, including components opened from another tab's toolbar. The selected component,
tab groups, vertical splits, sidebar width, and floating windows stay in place. Closing a
component hides it everywhere; any NMEA toolbar button reopens that same component. See
[Workspace and panels](workspace.md) for arranging components.

Configure instrument inputs in **Settings → Connections**. Choose **Live data source** in
the NMEA panel to read incoming messages; choosing **Loaded log** stops live reads and
selects the loaded recording. The recording button is only shown for live data. Saved
connection settings are kept when live reads stop.

Import interpreted `.nmea`, `.log`, `.txt`, and Actisense `.ebl` files through the
**Log library**. For NMEA 2000 recordings, select the interpreted `.log` companion when
available; W2K-1 EBL stacks can be dropped together and are stitched in numeric segment
order without altering the originals. Import runs in the background. Use **File Info**
in the library to inspect captured device identities and decode diagnostics. Drag or
click in an instrument chart to seek to that elapsed time; the chart cursor and GPS
marker follow the selected time. Fast-packet NMEA 2000 messages and unsupported PGNs
remain in the preserved EBL but are not fully decoded for replay.

Recording remains available while viewing live data. Switching to a loaded log stops an
active recording. Imported originals are retained in the managed log library; external
source files are never modified.

::::{note}
Recorded NMEA data is a record of what the instruments reported, not a validated
performance measurement. Instrument calibration, duplicate sensors and missing PGNs all
affect the result.
::::

Next steps
----------

- [Compare recorded performance with your polar](polars-performance.md).
- [Plan the passage you are about to sail](routing.md).
