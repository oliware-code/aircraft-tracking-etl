function airportIcon(iata) {
  const textShadow = 'text-shadow:0 0 2px #000,0 0 2px #000,0 0 3px #000;';
  return L.divIcon({
    className: '',
    html: `<div style="position:relative; width:4px; height:4px;">
             <div style="
               position:absolute; left:50%; top:-11px; transform:translateX(-50%);
               font-size:8px; font-weight:600; white-space:nowrap; color:#facc15; ${textShadow}
             ">${iata}</div>
             <div style="
               width:4px; height:4px; border-radius:50%;
               background:#facc15; border:1px solid #713f12;
               box-shadow:0 1px 2px rgba(0,0,0,0.6);
             "></div>
           </div>`,
    iconSize: [4, 4],
    iconAnchor: [2, 2],
    popupAnchor: [0, -6],
  });
}
