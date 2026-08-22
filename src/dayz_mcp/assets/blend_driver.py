"""The script Blender runs. Executed by `blender -b <file> --python <this>`.

**Never imported by the server.** It runs inside Blender's own interpreter,
where `bpy` exists and this package does not; importing it anywhere else
raises. It lives in the package rather than being generated as a string so
that it is real, readable, checkable code -- the alternative was a sixty-line
literal nobody's tooling ever looks at.

It takes one argument after `--`: a JSON file. It writes its answer to another
JSON file, named in that payload, and that file -- not the exit code, not the
log -- is what the caller reads. Blender's own exit code says nothing useful
here: the export operator returns `FINISHED` when it exported nothing at all.

Three things this script must get right, each of them measured:

* **The operator namespace is lazy.** `hasattr(bpy.ops.a3ob, "export_p3d")`
  answers **True** on an install where the add-on is not present at all --
  measured under `--factory-startup`, where the add-on list came back with
  eight entries and none of them the exporter, and the call then failed with
  `AttributeError: ... could not be found`. `dir()` is the honest question:
  in the same run it returned an EMPTY list.
* **The stored project root decides nothing.** It is set here, on the live
  preference object, from the value the caller declared -- and
  `use_preferences_save` is turned off first so this run cannot write it back.
  The root that was found on disk is reported so a caller can see what it
  would have been.
* **Preferences are never saved.** The person who owns this machine keeps
  their settings byte for byte.
"""
import json
import sys

import bpy

#: The add-on's own id. Matched against the LAST dotted segment of each key in
#: the preferences, because an extension's key carries the repository it came
#: from (`bl_ext.<repo>.<id>`) and a legacy install carries none of that.
ADDON_ID = "Arma3ObjectBuilder"
#: Where its operators live, and the one this server calls.
NAMESPACE = "a3ob"
OPERATOR = "export_p3d"


def payload_path(argv):
    """The JSON file named after `--`. Blender puts its own arguments before
    it and passes everything after it through untouched."""
    return argv[argv.index("--") + 1]


def addon_key(prefs):
    """This install's key for the add-on, or "" when it is not enabled."""
    wanted = ADDON_ID.lower()
    for key in prefs.addons.keys():
        if key.rsplit(".", 1)[-1].lower() == wanted:
            return key
    return ""


def lod_objects():
    """Every object the add-on considers a LOD.

    Counted so the caller can compare it against the number of LODs that
    actually reached the file. The exporter's `visible_only` defaults to True
    while LOD collections in a real source file are routinely hidden, and the
    result is an export of two LODs out of five with a success report.
    """
    found = []
    for obj in bpy.data.objects:
        props = getattr(obj, "a3ob_properties_object", None)
        if props is not None and getattr(props, "is_a3_lod", False):
            found.append(obj.name)
    return found


def run(payload, out):
    prefs = bpy.context.preferences
    # First, before anything is touched: this run may not write the owner's
    # preferences back to disk, and the root below is written into the live
    # preference object precisely because that is what the add-on reads.
    prefs.use_preferences_save = False

    operators = sorted(
        name for name in dir(getattr(bpy.ops, NAMESPACE)) if not name.startswith("_")
    )
    out["operators"] = operators
    key = addon_key(prefs)
    out["addon"] = key
    if OPERATOR not in operators or not key:
        out["error"] = (
            "the exporting add-on is not enabled in this Blender: %r is not among the %d "
            "operator(s) in bpy.ops.%s, and %r is not among the %d enabled add-on(s)"
            % (OPERATOR, len(operators), NAMESPACE, ADDON_ID, len(prefs.addons))
        )
        return

    addon_prefs = prefs.addons[key].preferences
    out["stored_root"] = str(getattr(addon_prefs, "project_root", ""))
    addon_prefs.project_root = payload["root"]
    out["root"] = str(addon_prefs.project_root)
    out["lods_in_blend"] = len(lod_objects())

    result = bpy.ops.a3ob.export_p3d(filepath=payload["output"], **payload["options"])
    out["operator_result"] = sorted(result)


def main():
    out = {
        "addon": "", "operators": [], "stored_root": "", "root": "",
        "lods_in_blend": None, "operator_result": [], "error": "",
    }
    path = ""
    try:
        payload = json.loads(
            open(payload_path(sys.argv), encoding="utf-8").read()  # noqa: SIM115
        )
        path = payload["result"]
        run(payload, out)
    except Exception as exc:  # noqa: BLE001 - the answer file is the report
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    if not path:
        # Nothing can be reported to a caller whose payload could not be read;
        # the log has the traceback, and the caller reads a missing answer
        # file as exactly what it is.
        raise SystemExit(2)
    with open(path, "w", encoding="utf-8") as fh:  # noqa: SIM115
        json.dump(out, fh, ensure_ascii=False, indent=2)


main()
