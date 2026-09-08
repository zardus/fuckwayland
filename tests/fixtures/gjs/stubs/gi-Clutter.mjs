// gi://Clutter: constants, and the grab the window picker takes.
//
// grabIsLive() in the bridge refuses a grab whose seat state is
// Clutter.GrabState.NONE -- a grab that took nothing is worse than none,
// because it answers no events and still has to be dismissed -- so the
// seat state a grab reports is settable per case.

import {record, tag} from './harness.mjs';

const Clutter = {
    EVENT_PROPAGATE: false,
    EVENT_STOP: true,
    KEY_Escape: 0xff1b,
    // ClutterEventType, read out of Clutter-14.typelib (gir1.2-mutter-14
    // 46.0-1ubuntu9) -- ENTER and LEAVE sit between MOTION and BUTTON_PRESS,
    // so BUTTON_PRESS is 6 and BUTTON_RELEASE 7, and the picker's
    // _onSelectEvent() compares against these numbers.  Clutter-18
    // (gir1.2-mutter-18 50.1-0ubuntu2.2) agrees up to PAD_RING and then
    // inserts PAD_DIAL at 22, pushing DEVICE_ADDED..IM_PREEDIT up by one and
    // adding KEY_STATE 28; nothing the bridge reads moves, so one table
    // serves both and the divergence is written down here instead.
    EventType: {
        NOTHING: 0, KEY_PRESS: 1, KEY_RELEASE: 2, MOTION: 3, ENTER: 4,
        LEAVE: 5, BUTTON_PRESS: 6, BUTTON_RELEASE: 7, SCROLL: 8,
        TOUCH_BEGIN: 9, TOUCH_UPDATE: 10, TOUCH_END: 11, TOUCH_CANCEL: 12,
        TOUCHPAD_PINCH: 13, TOUCHPAD_SWIPE: 14, TOUCHPAD_HOLD: 15,
        PROXIMITY_IN: 16, PROXIMITY_OUT: 17, PAD_BUTTON_PRESS: 18,
        PAD_BUTTON_RELEASE: 19, PAD_STRIP: 20, PAD_RING: 21,
        DEVICE_ADDED: 22, DEVICE_REMOVED: 23, IM_COMMIT: 24, IM_DELETE: 25,
        IM_PREEDIT: 26, EVENT_LAST: 27,
    },
    GrabState: {NONE: 0, POINTER: 1 << 0, KEYBOARD: 1 << 1,
                ALL: (1 << 0) | (1 << 1)},

    // ClutterCursorType, new in Clutter-18: mutter 18 dropped Meta.Cursor and
    // moved the cursor names here, with CSS spellings and different numbers
    // (CROSSHAIR 9 against Meta.Cursor.CROSSHAIR 17).  The bridge still reads
    // Meta.Cursor.CROSSHAIR inside safe(), so on GNOME 50 the crosshair is
    // simply never set; a case that wants to model that deletes Meta.Cursor.
    CursorType: {
        INHERIT: 0, NONE: 1, DEFAULT: 2, CONTEXT_MENU: 3, HELP: 4, POINTER: 5,
        PROGRESS: 6, WAIT: 7, CELL: 8, CROSSHAIR: 9, TEXT: 10,
    },

    /** A Clutter.Grab double; `seatState` null means it has no get_seat_state. */
    makeGrab(seatState = 3) {
        const g = tag({}, `Grab(${seatState})`);
        if (seatState !== null)
            g.get_seat_state = () => record('grab.get_seat_state', [], seatState);
        g.dismiss = () => record('grab.dismiss', []);
        return g;
    },
};

export default Clutter;
