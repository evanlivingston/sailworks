Quickstart
==========

Installation
------------

The recommended way to install GWeatherRouting is via **Flatpak** from Flathub:

```
flatpak install flathub org.gweatherrouting.gweatherrouting
```

Once installed, you can run it with:

```
flatpak run org.gweatherrouting.gweatherrouting
```

You can also browse the app page on [Flathub](https://flathub.org/apps/org.gweatherrouting.gweatherrouting).

### Alternative: AppImage

You can download the latest AppImage from [GitHub releases](https://github.com/dakk/gweatherrouting/releases). After downloading, make it executable:

```
chmod +x GWeatherRouting-linux-x86_64.AppImage
```

<img src="../_static/images/quickstart/1.png">

### Alternative: Windows

Download the latest [Windows portable ZIP](https://github.com/dakk/gweatherrouting/releases/download/v0.3.6/GWeatherRouting-win64.zip), extract it, and run `GWeatherRouting.bat` (or `GWeatherRouting-noconsole.vbs` to hide the console window).

### Alternative: From source

You can install gweatherrouting from source, following the instructions in the repository [README.md](https://github.com/dakk/gweatherrouting).


First run
---------

During the first run, gww will ask you to download some files:
- Base world countourn lines
- OpenSeaMap data: contains lighthouses, buoys, and other maritime signals taken from the [openseamap project](https://openseamap.org/index.php?id=openseamap&no_cache=1)

<img src="../_static/images/quickstart/2.png">

After these download, gww is ready to be used.


<img src="../_static/images/quickstart/3.png">


Downloading a grib file
-----------------------

1. Click on the grib manager button

<img src="../_static/images/quickstart/4.png">

2. Select a region, right-click and download 

<img src="../_static/images/quickstart/5.png">

3. After closing the grib manager, the grib file is loaded into the map. You can freely inspect the wind for different time slots.

<img src="../_static/images/quickstart/6.gif">



Track creation and editing
--------------------------

To create a track and add a point to it, right-click in a point of the map and press "Add to Track". This
will create a new track, adding the clicked point to it.

<img src="../_static/images/quickstart/7.png">

In order to add new track points, right-click on another point of the map and press "Add to Track" as before.
On the left pane, you will se the list of the tracks, and the list of track point for each track. 
You can:
- Remove or Export tracks, by right-clicking on a track
- Remove, Reorder or Duplicate a track point, by right-clicking on a track point


<img src="../_static/images/quickstart/8.png">


Create a routing
----------------

Now that we have a track and a grib, creating a routing is very easy. Just click on "Route..." button.
Select a polar file, tune other settings and press run.

<img src="../_static/images/quickstart/9.png">

The software will calculate the isochrones tree, finding the fastest route. After calculation, the route is displayed in red.
The green point on the routing shows the boat position at different times. You can navigate the simulation through time, using
time control's buttons.


<img src="../_static/images/quickstart/10.gif">

Recording NMEA 2000 instruments
------------------------------

The app reads complete NMEA 2000 PGNs from gateways that provide Actisense BST
or Actisense N2K ASCII output. The NGT-1 uses BST; an NGX-1 must be in transfer
mode, and its hardware compatibility has not yet been verified. N2K ASCII
gateways must reassemble fast and ISO transport packets before sending them to
the app. These connections are input only: Actisense BST receives one
gateway-local receive-mode command at connection time, but the app does not send
vessel-control or NMEA 2000 transmit messages.

1. Connect the gateway to the powered NMEA 2000 network and your computer.
2. In **Settings > Connections**, set **Protocol** to **NMEA 2000**, then
   choose the **Gateway data format** your gateway provides. For an Actisense
   NGT-1, choose **Actisense BST**. For a gateway configured to output
   **N2K ASCII**, choose that format; it can use a serial port or a TCP/UDP
   network connection.
3. For a serial gateway, select its serial port (on macOS, normally
   `/dev/cu.usbserial-...`). If your
   operating system does not expose the port, install the appropriate Actisense
   USB driver. The Actisense BST connection defaults to **230400** baud; use
   **115200** for an older/configured interface. Set the gateway's output format
   to N2K ASCII before selecting that connection type.
4. Save the connection and check that instrument readings and its packet count
   update. In **Logs**, click **Record**, then **Stop** and **Save** after sailing.

Saving `sail.nmea` also saves `sail.nmea.n2k.log` when NMEA 2000 messages have been
received. The first file contains converted NMEA 0183 sentences for the existing
Logs display. The companion contains validated original PGN payloads in CANboat
raw text format, with UTC receipt timestamps, priorities, source/destination
addresses and PGN numbers. Unknown/proprietary PGNs are retained there too.
The companion is a complete session archive: cropping in Logs affects only the
converted NMEA file. Save each sail before starting another recording or using
Clear; the working recording is temporary and a new session replaces it.

Supported display conversions include heading (127250), speed through water
(128259), depth (128267), GPS position (129025/129029), COG/SOG (129026), wind
(130306) and sea temperature (130310/130312/130316). Geographic wind directions
and ground-referenced true wind are kept in the original archive rather than
converted into water-referenced wind angles. Position sentences use the Mac/PC
UTC receipt time; synchronize its clock before sailing. Original GNSS/system
clock data remain available in the PGN archive.

The Logs speed graph uses GPS speed over ground. Its existing true-wind
estimation is a review aid, not a calibrated polar calculation. Use the original
wind and water-speed PGNs for performance analysis. The converted file is not
yet a verified Polarize import; raw-file conversion and source selection need
validation against an actual recording. The display consumes supported
measurements from all received sources, so duplicate sensors may alternate;
the raw archive preserves source addresses for later selection.

### Try it without a boat

On macOS or Linux, run this from the source checkout in a separate terminal:

```sh
python3 tools/simulate_actisense.py
```

The simulator prints a pseudo-terminal path, such as `/dev/ttys012`. Enter that
path in the connection dialog's serial-port field, choose **NMEA 2000** and
**Actisense BST**, set 230400 baud, and leave the simulator running. It sends
synthetic sailing data near Vancouver: GPS position, heading, wind, water speed,
depth and temperature. You can watch the dashboard and make/save a Logs
recording through the same binary input path used by the real interface. Stop
with Ctrl-C, then remove the simulated connection before connecting the boat.
`--duration 60` runs a one-minute simulation. The simulator needs only Python's
standard library; Windows pseudo-terminals are not supported.
