/*
 * GNOME Shell extension that exposes the active window title to Kakarot.
 *
 * Interface: org.kakarot.Context
 * Path:      /org/kakarot/Context
 * Method:    GetActiveTitle() -> s
 *
 * This is the GNOME 45+ ESM extension format. GNOME 44 and older use the
 * legacy imports.gi loader, which cannot load this file.
 */

import Gio from 'gi://Gio';

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.kakarot.Context';
const OBJECT_PATH = '/org/kakarot/Context';

const IFACE_XML = `
<node>
  <interface name="${BUS_NAME}">
    <method name="GetActiveTitle">
      <arg type="s" direction="out" name="title"/>
    </method>
  </interface>
</node>`;

/**
 * The JavaScript object bound to the D-Bus interface.
 *
 * Gio.DBusExportedObject maps the D-Bus method names onto this instance's
 * methods. Keep this separate from the Extension instance so the D-Bus
 * surface stays small and explicit.
 */
class ContextService {
    GetActiveTitle() {
        try {
            const window = global.display.focus_window;
            if (window && typeof window.title === 'string' && window.title.length > 0)
                return window.title;
        } catch (error) {
            console.debug(`Kakarot Context: failed to read the focused window: ${error}`);
        }

        return 'Desktop';
    }
}

export default class KakarotContextExtension extends Extension {
    constructor(metadata) {
        super(metadata);

        this._enabled = false;
        this._bus = null;
        this._dbus = null;
        this._nameOwnerId = 0;
        this._service = null;

        // GNOME 45+ does not call init() itself; do it here so the extension
        // also follows the traditional init/enable/disable lifecycle.
        this.init();
    }

    init() {
        if (this._enabled)
            return;

        this._teardown();
    }

    enable() {
        if (this._enabled)
            return;

        this._enabled = true;

        try {
            this._bus = Gio.DBus.session;
            if (!this._bus)
                throw new Error('the session bus connection is unavailable');

            this._service = new ContextService();
            this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this._service);

            // Export before owning the well-known name so a caller that sees
            // org.kakarot.Context can never race ahead of the object path.
            this._dbus.export(this._bus, OBJECT_PATH);

            this._nameOwnerId = Gio.bus_own_name_on_connection(
                this._bus,
                BUS_NAME,
                Gio.BusNameOwnerFlags.NONE,
                () => console.debug(`Kakarot Context: acquired ${BUS_NAME}`),
                () => console.warn(`Kakarot Context: lost ${BUS_NAME}; ` +
                    'another process may own the name'));
        } catch (error) {
            console.error(`Kakarot Context: could not enable D-Bus export: ${error}`);
            this._teardown();
        }
    }

    disable() {
        this._teardown();
    }

    _teardown() {
        this._enabled = false;

        if (this._nameOwnerId) {
            try {
                Gio.bus_unown_name(this._nameOwnerId);
            } catch (error) {
                console.debug(`Kakarot Context: failed to release ${BUS_NAME}: ${error}`);
            }
            this._nameOwnerId = 0;
        }

        if (this._dbus) {
            try {
                this._dbus.unexport();
            } catch (error) {
                console.debug(`Kakarot Context: failed to unexport ${OBJECT_PATH}: ${error}`);
            }
            this._dbus = null;
        }

        this._bus = null;
        this._service = null;
    }
}
