function planeIcon(onGround, heading, label, epoch, route, trackedBy) {
  // Colors tuned for contrast on a dark basemap (CartoDB dark_all).
  // Callsign-tracked aircraft get orange instead of blue while airborne, so the two
  // watchlists are visually distinguishable on the map at a glance.
  const airborneColor = trackedBy === 'callsign' ? '#fb923c' : '#38bdf8';
  const airborneStroke = trackedBy === 'callsign' ? '#7c2d12' : '#0c4a6e';
  const color = onGround ? '#f8fafc' : airborneColor;
  const stroke = onGround ? '#0f172a' : airborneStroke;
  const rotation = heading || 0;
  const textShadow = 'text-shadow:0 0 3px #000,0 0 3px #000,0 0 4px #000,0 0 4px #000;';
  const labelHtml = label
    ? `<div style="
         position:absolute; left:50%; top:-18px; transform:translate(-50%,-100%);
         font-size:11px; font-weight:600; white-space:nowrap; color:#f8fafc; ${textShadow}
       ">${label}</div>`
    : '';
  const routeHtml = route
    ? `<div style="
         position:absolute; left:50%; top:-4px; transform:translate(-50%,-100%);
         font-size:9px; font-weight:600; white-space:nowrap; color:#facc15; ${textShadow}
       ">${route}</div>`
    : '';
  const timerHtml = epoch != null
    ? `<div class="live-since live-since-compact" data-epoch="${epoch}" style="
         position:absolute; left:50%; top:32px; transform:translateX(-50%);
         font-size:9px; font-weight:600; white-space:nowrap; color:#f8fafc; ${textShadow}
       ">--:--:--</div>`
    : '';
  return L.divIcon({
    className: '',
    html: `<div style="position:relative; width:30px; height:30px;">
             ${labelHtml}
             ${routeHtml}
             <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="30" height="30"
               style="transform: rotate(${rotation}deg); filter: drop-shadow(0 1px 3px rgba(0,0,0,0.8));">
               <path fill="${color}" stroke="${stroke}" stroke-width="0.6"
                 d="M21 16v-2l-8-5V3.5C13 2.67 12.33 2 11.5 2S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2.5 1.5V22l4-1 4 1v-1.5L13 19v-5.5l8 2.5z"/>
             </svg>
             ${timerHtml}
           </div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -15],
  });
}
