// resource:///org/gnome/shell/ui/main.js
//
// `modalCount` is the one field that decides a refusal: the overlap
// extension will not write while anything holds a modal grab, because
// "Keep changes?" is one of the things that does, and a saved overlap
// poisons ~/.config/monitors.xml for good.  It is a live `let` binding with
// a setter, so a case sets it and the extension reads the new value.
//
// The rest is what the bridge's window picker touches: an actor group to
// park a grab actor in, pushModal/popModal, and the overview's visibility.

import {record, tag} from './harness.mjs';

export let modalCount = 0;
export let actionMode = 1;               // Shell.ActionMode.NORMAL

export function setModalCount(n) {
    modalCount = n;
}

export function setActionMode(m) {
    actionMode = m;
}

export const layoutManager = {
    monitors: [],
    primaryIndex: 0,
    modalDialogGroup: tag({
        children: [],
        add_child(a) {
            record('modalDialogGroup.add_child', []);
            this.children.push(a);
        },
        remove_child(a) {
            record('modalDialogGroup.remove_child', []);
            this.children = this.children.filter(c => c !== a);
        },
    }, 'modalDialogGroup'),
};

export const uiGroup = tag({
    children: [],
    add_child(a) {
        record('uiGroup.add_child', []);
        this.children.push(a);
    },
    remove_child(a) {
        record('uiGroup.remove_child', []);
        this.children = this.children.filter(c => c !== a);
    },
}, 'uiGroup');

export const overview = {visible: false};
export const sessionMode = {currentMode: 'user'};
export const screenShield = null;
export const wm = tag({}, 'wm');

/** What pushModal() hands back; null makes the bridge fall through to a stage grab. */
export let modalGrab = tag({}, 'ModalGrab');

export function setModalGrab(g) {
    modalGrab = g;
}

export function pushModal(actor, params) {
    return record('Main.pushModal', [params && params.actionMode], modalGrab);
}

export function popModal(grab) {
    record('Main.popModal', []);
}

export function notify(msg) {
    record('Main.notify', [msg]);
}

/**
 * Put the module back the way it was imported.  A monitor is
 * {x, y, width, height, index, geometry_scale}, which is what
 * layoutManager hands the bridge for ListMonitors.
 */
export function reset() {
    modalCount = 0;
    actionMode = 1;
    layoutManager.monitors = [];
    layoutManager.primaryIndex = 0;
    layoutManager.modalDialogGroup.children = [];
    uiGroup.children = [];
    overview.visible = false;
    modalGrab = tag({}, 'ModalGrab');
}
