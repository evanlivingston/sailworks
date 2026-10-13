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


### Chart navigation on macOS

Pinch the trackpad to zoom around the pointer. Scroll with two fingers to pan
the chart horizontally or vertically; macOS scroll momentum is preserved.
Control-scroll or Command-scroll also zooms. A mouse wheel zooms as before.

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

### Restore your workspace between launches

The app automatically remembers its active tab, Charts and Logs map positions
and zoom, chart display toggles, Logs graph selections, selected polar and
performance filters, and live-stream and playback-speed preferences. It also
restores sidebar components, selected component tabs, groups, divider positions,
sidebar width, floating-window geometry, and the main window's normal size and
maximized state. Polar Manager's selected tab and ORC search text are remembered.

View changes are saved after a short idle period and again on normal exit.
Saving waits while a map is being panned or zoomed. Workspace preferences use a
separate versioned `workspace.json` in the app's data directory (on macOS,
`~/Library/Application Support/gweatherrouting/`). Files are replaced atomically;
missing or unreadable workspace files fall back to the default view. Saved
windows are fitted onto the available monitors when the display arrangement changes.

Sample history, loaded NMEA files, recording state, and playback position are
session-only. Launching starts with live input selected and the saved streaming
preference; it does not reopen or automatically play a log. Existing settings,
connections, tracks, routes, and downloaded data retain their own storage.

### Shared NMEA streaming and playback

Open **NMEA** from the toolbar in **Charts**, **Logs**, or **Polars**. All three
tabs share one sidebar and one NMEA component. Open components stay visible
when switching tabs, including components opened from another tab's toolbar.
The selected component, tab groups, vertical splits, sidebar width, and floating
windows stay in place. Closing a component hides it everywhere; any NMEA toolbar
button reopens that same component.

Source selection, live streaming, loaded log, play/pause, playback position,
and speed also persist when switching tabs, hiding panels, or floating them.
The session stays in memory until the app closes.
Configure instrument inputs in **Settings > Connections**, then choose
**Live connections** and enable **Stream live NMEA**.

Use **Load NMEA log…** to open an interpreted `.nmea`, `.log`, or `.txt` file.
Select the primary interpreted file for NMEA 2000 recordings, rather than its
raw `.n2k.log` companion. Loading runs in the background and shows the full
track in Logs and measured samples in Polars. **Play** starts from the beginning
and feeds the replay to all three tabs; **Pause** retains the position, and
**Restart** clears replay progress. Choose a playback speed from 0.5× to 10×.
Playback uses NMEA UTC timestamps, including midnight rollover. Logs without
timestamps use ten sentences per second, stated in the panel. Files are limited
to one million valid sentences. Incoming live data is excluded while a log is
selected. Return to **Live connections** to resume instrument input.

Recording and saving remain in **Logs**. Changing away from live input or
pausing its stream stops an active recording. Loading and playing a log does
not overwrite the working recording. Cropping a loaded log filters its plotted
track and subsequent saved copy; the original file and raw archive are preserved.

### Compare actual performance with your polars

In **Polars**, select a polar and open **Performance** from the toolbar.
The controls live in a side panel: drag its tab or grip out to float it, or
drag the floating panel to the right edge to dock it again. Closing the panel
hides its controls while preserving samples, settings, live collection, and
the plotted overlay. Reopen it using the toolbar button.

Enable **Show actual performance** in the panel. Solid curves show predicted boat speed; dots show measured speed at each true wind
angle, preserving port and starboard tacks. Both use the same radial boat-speed
scale. Dot colors match the nearest polar true-wind-speed curve, with band
boundaries halfway between neighboring wind speeds. Measurements outside the
polar's wind-speed range are excluded. Use the wind-speed selector to isolate
one band. Hover over a curve to immediately see its true wind speed and angle;
other curves dim until you move away. Hover over a dot to inspect its measurements.

**Live connections** collects valid instrument true-wind reports (MWV with reference
T) paired with speed readings no more than five seconds old. Choose **Speed
through water** for a direct performance comparison, or **Speed over ground**
to include current effects. These sources are kept separate; a missing water
speed is never replaced with GPS speed. Apparent wind is not converted into
true wind for this overlay. Supported NMEA 2000 inputs use the same instrument
conversions described above.

**Load NMEA log…** opens a saved interpreted NMEA log, including the primary
file saved from Logs. Select that file rather than its raw `.n2k.log` companion.
These interpreted logs have no receipt timestamps, so import pairs each wind
report with speed within the preceding 50 lines instead of applying a time
limit. Logs without instrument true wind or a usable speed reading produce no
points. Loaded samples appear immediately; during playback the overlay grows
with the replay.

Live collection retains the latest 20,000 samples in memory, with at most one
sample per second per speed basis. Import retains the latest 20,000 samples
without time resampling. **Stream live NMEA** in the shared NMEA panel pauses or resumes input;
**Clear samples** in Performance clears the selected live/log history. The comparison
uses the same samples when you select a different polar, so clear the history
when changing boats or sail configurations. Save recordings through **Logs**
to keep them across app restarts.

### Manage polars in a component

Click **Polar manager** in Polars or the routing wizard to open its component.
It shares the sidebar and floating-window behavior of NMEA and Performance, and
keeps its search, selection, and loaded catalog when hidden and reopened.

In **Local polars**, use **Open polar…** to import a `.pol` file, toggle its
**Enabled** checkbox, or select a file and click **Remove**. In **ORC catalog**,
search by sail number, boat name, or type, select a result, and click **Download
selected polar**. Already downloaded entries are disabled. Local polar lists
and the routing wizard update as changes happen; closing the component is not
required to refresh them.

### Select a polar in a component

Use **Polar selector** in the Polars toolbar to show or hide the polar list.
The selector can dock alongside the other components or float in its own
window. Selecting a row updates the chart and speed table immediately.
Your selection survives hiding, floating, redocking, and polar-list updates;
if the selected polar is removed or disabled, the first available polar is
selected instead. The speed table remains beside the chart.

### Stack components vertically

Drag a component’s named tab to the upper or lower part of another component
below its tab strip in the sidebar. A blue horizontal line marks where it will be placed; release to
create a vertical stack. Drag the divider between groups to adjust their
heights. Each group can still contain multiple tabs.

Drop on a group's tab strip or middle to combine components as tabs. The dotted grip
moves a whole tab group: drag it out to float, or back over the sidebar to
place that group above, below, or inside another group. Hiding a group keeps
its component state; reopening a component restores its place in the stack.
