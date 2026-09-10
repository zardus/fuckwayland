#!/bin/bash
# layout-5, in the GNOME guest as user `test`:  $1 = before|after
T=$1
cd /home/test/$T || exit 2
export PYTHONPATH=/home/test/$T W11_PASSTHROUGH=never
W="python3 -m wxrandr"
echo "########## TREE=$T ##########"
$W --output Virtual-1 --auto --pos 0x0 \
   --output Virtual-2 --auto --right-of Virtual-1 \
   --output Virtual-3 --auto --right-of Virtual-2 >/dev/null 2>&1
sleep 1
$W --output Virtual-2 --primary >/dev/null 2>&1
sleep 1
echo "after --output Virtual-2 --primary:"
echo "  wxrandr: $($W --query | grep primary | cut -d' ' -f1)"
echo "  xrandr : $(xrandr --query 2>/dev/null | grep ' primary' | cut -d' ' -f1)"
$W --output Virtual-2 --same-as Virtual-1 --output Virtual-3 --same-as Virtual-2 >/dev/null 2>&1
sleep 1
echo "after mirroring Virtual-2 and Virtual-3 onto Virtual-1:"
echo "  wxrandr      : $($W --query | grep primary | cut -d' ' -f1)"
echo "  xrandr       : $(xrandr --query 2>/dev/null | grep ' primary' | cut -d' ' -f1)"
echo "  listmonitors : $($W --listmonitors | sed -n 2p)"
echo "  state file   : $(cat $XDG_RUNTIME_DIR/wxrandr-state.json 2>/dev/null)"
$W --output Virtual-2 --right-of Virtual-1 --output Virtual-3 --right-of Virtual-2 >/dev/null 2>&1
sleep 1
echo "after un-mirroring:"
echo "  wxrandr: $($W --query | grep primary | cut -d' ' -f1)"
echo "  xrandr : $(xrandr --query 2>/dev/null | grep ' primary' | cut -d' ' -f1)"
echo "--- layout-8: the message for a hole in the row ---"
$W --output Virtual-2 --off 2>&1 | head -2 | sed 's/^/    /'
$W --output Virtual-2 --auto --right-of Virtual-1 --output Virtual-3 --right-of Virtual-2 >/dev/null 2>&1
