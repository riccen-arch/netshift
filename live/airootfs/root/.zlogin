# netshift Live: auf tty1 automatisch die Oberfläche starten (andere Konsolen: Alt+F2 …)
if [[ "$(tty)" == /dev/tty1 && -z "$NETSHIFT_NO_GUI" ]]; then
    netshift-live-session
fi
