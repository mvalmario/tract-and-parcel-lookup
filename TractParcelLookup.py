"""
Choose Tract Map (TR) or Parcel Map (PM), enter a map number,
and this will:
    1) Select the matching feature in the active ArcGIS Pro map
       (DPW.Tract_Map / DPW.Parcel_Map), read its REFERENCE field, and
       build the direct PDF URL for that map (no web form involved).
    2) Open that PDF in a Chrome window at 100% zoom, sidebar collapsed,
       maximized on the second monitor.
    3) Zoom the ArcGIS Pro map to the selected feature.

Each step is toggled by the "Open map pdf" / "Zoom in map"
checkboxes, and choose which monitor will display the pdf with
"""

import re
import time
import tkinter as tk
from tkinter import scrolledtext

import arcpy

try:
    from selenium import webdriver
    SELENIUM_AVAILABLE = True
except ImportError:
    from selenium import webdriver
    SELENIUM_AVAILABLE = False

try:
    import screeninfo
    SCREENINFO_AVAILABLE = True
except ImportError:
    import screeninfo
    SCREENINFO_AVAILABLE = False



# Configure layers

LAYER_OPTIONS = {
    "tract map": {
        "layer_name": "Tract Map",
        "prefix": "TR",
    },
    "parcel map": {
        "layer_name": "Parcel Map",
        "prefix": "PM",
    },
}

FIELD_NAME = "SUB_NAME"

# ArcGIS Pro helpers

def find_layer(map_obj, target_name):
    """Find a layer by exact name or short name, skipping unreadable layers."""
    target_lower = target_name.lower()
    target_short = target_name.split(".")[-1].lower()

    for lyr in map_obj.listLayers():
        try:
            lyr_name = lyr.name
        except AttributeError:
            continue

        lyr_lower = lyr_name.lower()
        lyr_short = lyr_name.split(".")[-1].lower()

        if lyr_lower == target_lower or lyr_short == target_short:
            return lyr

    raise RuntimeError(f"Could not find a layer named '{target_name}' in the active map.")


def get_field_name(layer, desired_name):
    """Return the layer's actual field name matching desired_name, case-insensitively."""
    desired_lower = desired_name.lower()
    for field in arcpy.ListFields(layer):
        if field.name.lower() == desired_lower:
            return field.name

    raise RuntimeError(
        f"Could not find a field named '{desired_name}' (case-insensitive) "
        f"on layer '{layer.name}'."
    )


def sql_quote(value):
    return "'" + value.replace("'", "''") + "'"


def normalize_map_number(raw_value):
    """Normalize user input into the suffix portion only"""
    map_number = raw_value.upper().strip()
    map_number = re.sub(r"\s+", "", map_number)
    map_number = re.sub(r"^(TR|PM)", "", map_number)

    if not re.fullmatch(r"\d{4,5}(-\d{2})?", map_number):
        raise ValueError("Enter a map number like 88475, 88475-01, or 88475-02.")

    return map_number


def resolve_layer_and_subname(map_view, map_type_key, map_number):
    """Shared setup: find the target layer and build the SUBNAME value to match."""
    layer_info = LAYER_OPTIONS[map_type_key]
    target_layer_name = layer_info["layer_name"]
    prefix = layer_info["prefix"]
    target_subname = f"{prefix}{map_number}"

    active_map = map_view.map
    target_layer = find_layer(active_map, target_layer_name)

    return target_layer, target_subname, prefix


def select_and_zoom(map_view, map_type_key, map_number, log):
    """Run the select-by-attribute + zoom against a previously captured map view."""
    target_layer, target_subname, prefix = resolve_layer_and_subname(
        map_view, map_type_key, map_number
    )

    active_map = map_view.map
    for option in LAYER_OPTIONS.values():
        try:
            lyr = find_layer(active_map, option["layer_name"])
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
        except RuntimeError:
            pass

    actual_field_name = get_field_name(target_layer, FIELD_NAME)
    field_sql = arcpy.AddFieldDelimiters(target_layer.dataSource, actual_field_name)
    where_clause = f"{field_sql} = {sql_quote(target_subname)}"

    log(f"Selecting from {target_layer.name} with: {where_clause}")

    result = arcpy.management.SelectLayerByAttribute(target_layer, "NEW_SELECTION", where_clause)
    selected_count = int(result.getOutput(1))

    if selected_count == 0:
        log(f"WARNING: No features found for {target_subname}.")
        return

    selected_extent = map_view.getLayerExtent(target_layer, True, True)
    map_view.camera.setExtent(selected_extent)
    map_view.camera.scale *= 1.15

    log(f"Selected and zoomed to {selected_count} feature(s) for {target_subname}.")

    for option in LAYER_OPTIONS.values():
        try:
            lyr = find_layer(active_map, option["layer_name"])
            arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
        except RuntimeError:
            pass

def get_reference_value(map_view, map_type_key, map_number, log):
    """Look up the REFERENCE field for the feature matching this map number."""
    target_layer, target_subname, prefix = resolve_layer_and_subname(
        map_view, map_type_key, map_number
    )

    actual_subname_field = get_field_name(target_layer, FIELD_NAME)
    actual_reference_field = get_field_name(target_layer, "REFERENCE")

    field_sql = arcpy.AddFieldDelimiters(target_layer.dataSource, actual_subname_field)
    where_clause = f"{field_sql} = {sql_quote(target_subname)}"

    with arcpy.da.SearchCursor(target_layer, [actual_reference_field], where_clause) as cursor:
        for row in cursor:
            return row[0]

    log(f"WARNING: No REFERENCE value found for {target_subname}.")
    return None


def build_pdf_url(map_type_key, reference_value):
    """Build the direct PDF URL from a REFERENCE value"""

    prefix = LAYER_OPTIONS[map_type_key]["prefix"]  # "TR" or "PM"
    remainder = reference_value[len(prefix):]  # e.g. "1455-009"
    book_number = remainder.split("-")[0]  # e.g. "1455"

    if map_type_key == "tract map":
        return f"https://pw.lacounty.gov/sur/nas/landrecords/tract/MB{book_number}/{reference_value}.pdf"
    else:
        return f"https://pw.lacounty.gov/sur/nas/landrecords/parcel/PM{book_number}/{reference_value}.pdf"


# ---------------------------------------------------------------------------
# Web automation
# ---------------------------------------------------------------------------

class BrowserSession:
    """Keeps one Chrome window open and reuses it across searches."""

    def __init__(self):
        self.driver = None

    def get_driver(self, log=None):
        if self.driver is not None:
            try:
                _ = self.driver.window_handles
            except Exception:
                self.driver = None
                if log:
                    log("Browser window was closed -- opening a new one.")

        if self.driver is None:
            self.driver = webdriver.Chrome()
        return self.driver

    def open_pdf(self, pdf_url, log, chosen_screen):
        driver = self.get_driver(log)
        driver.get(f"{pdf_url}#view=FitH&navpanes=0")
        time.sleep(1)

        log(f"Opened {pdf_url} at 100% zoom with sidebar collapsed.")

        self._position_on_second_monitor(driver, log, chosen_screen)

    def _position_on_second_monitor(self, driver, log, chosen_screen):
        if not SCREENINFO_AVAILABLE:
            log("screeninfo not installed -- leaving window where it is. "
                "Run: pip install screeninfo")
            return

        try:
            monitors = screeninfo.get_monitors()
        except Exception as exc:
            log(f"Could not detect monitors: {exc}")
            return

        if len(monitors) < 2:
            log("Only one monitor detected -- leaving window on the primary monitor.")
            return


        monitors_sorted = sorted(monitors, key=lambda m: m.x)
        chosen_monitor = monitors_sorted[chosen_screen]

        # Move the window onto the chosen monitor first, then maximize
        driver.set_window_position(chosen_monitor.x, chosen_monitor.y)
        driver.set_window_size(chosen_monitor.width, chosen_monitor.height)
        driver.maximize_window()

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui():
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    map_view = aprx.activeView

    if map_view is None or not hasattr(map_view, "map") or not hasattr(map_view, "getLayerExtent"):
        arcpy.AddError(
            "Open and activate a map view in ArcGIS Pro before running this tool "
            "(click into the map so it's the active pane, then run the tool)."
        )
        return

    browser = BrowserSession()

    root = tk.Tk()
    root.title("Select, Zoom, and Look Up Map")
    root.geometry("")

    map_type_var = tk.StringVar(value="tract map")
    screenchoose_var = tk.IntVar(value=0)
    zoom_var = tk.IntVar(value=1)
    openpdf_var = tk.IntVar(value=1)

    frame_top = tk.Frame(root)
    frame_top.pack(pady=10)

    tk.Label(frame_top, text="Map Type:").grid(row=0, column=0, sticky="w", padx=5)
    tk.Radiobutton(frame_top, text="Tract Map (TR)", variable=map_type_var,
                    value="tract map").grid(row=0, column=1, sticky="w")
    tk.Radiobutton(frame_top, text="Parcel Map (PM)", variable=map_type_var,
                    value="parcel map").grid(row=0, column=2, sticky="w")

    tk.Label(frame_top, text="Map Number:").grid(row=1, column=0, sticky="w", padx=5, pady=8)
    number_entry = tk.Entry(frame_top, width=20)
    number_entry.grid(row=1, column=1, columnspan=2, sticky="w")
    number_entry.focus()


    tk.Label(frame_top, text="Options:").grid(row=2, column=0, sticky="w", padx=5)
    tk.Checkbutton(frame_top, text="Zoom to feature", variable=zoom_var).grid(row=2, column=1, sticky="w")
    tk.Checkbutton(frame_top, text="Open map pdf", variable=openpdf_var).grid(row=2, column=2, sticky="w")

    monitors = screeninfo.get_monitors()
    if len(monitors) > 1:
        tk.Label(frame_top, text="Open pdf on monitor:").grid(row=3, column=0, sticky="w", padx=5)
        for n, monitor in enumerate(monitors):
            display_label = f"{n+1}"
            tk.Radiobutton(frame_top, text=display_label, variable=screenchoose_var,
                   value=n).grid(row=int(n/2)+3, column=(n&1)+1, sticky="w")

    log_box = scrolledtext.ScrolledText(root, width=58, height=14, state="disabled")
    log_box.pack(padx=10, pady=10)

    def log(message):
        log_box.configure(state="normal")
        log_box.insert(tk.END, message + "\n")
        log_box.see(tk.END)
        log_box.configure(state="disabled")
        root.update_idletasks()

    def on_go():
        map_type_key = map_type_var.get()
        raw_number = number_entry.get()
        chosen_screen = screenchoose_var.get()

        try:
            map_number = normalize_map_number(raw_number)
        except ValueError as exc:
            log(f"ERROR: {exc}")
            return

        if not zoom_var.get() and not openpdf_var.get():
            log("Nothing to do -- check at least one option (Zoom in map / Open map pdf).")
            return

        # Step 1: ArcGIS Pro select + zoom (only if "Zoom in map" is checked)
        if zoom_var.get():
            try:
                select_and_zoom(map_view, map_type_key, map_number, log)
            except Exception as exc:
                log(f"ERROR during select/zoom: {exc}")

        # Step 2: open the PDF directly (only if "Open map pdf" is checked)
        if openpdf_var.get():
            if SELENIUM_AVAILABLE:
                try:
                    reference_value = get_reference_value(map_view, map_type_key, map_number, log)
                    if reference_value:
                        pdf_url = build_pdf_url(map_type_key, reference_value)
                        browser.open_pdf(pdf_url, log, chosen_screen)
                except Exception as exc:
                    log(f"ERROR opening PDF: {exc}")
            else:
                log("Selenium is not installed in this Python environment -- "
                    "skipping PDF open. See the setup instructions at the "
                    "top of this script.")

    button_frame = tk.Frame(root)
    button_frame.pack(pady=5)

    tk.Button(button_frame, text="Go", width=12, command=on_go).grid(row=0, column=0, padx=5)
    tk.Button(button_frame, text="Close", width=12,
              command=lambda: (browser.close(), root.destroy())).grid(row=0, column=1, padx=5)

    root.protocol("WM_DELETE_WINDOW", lambda: (browser.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    run_gui()
