function airportIcon(iata) {
  const textShadow = 'text-shadow:0 0 2px #000,0 0 2px #000,0 0 3px #000;';
  return L.divIcon({
    className: '',
    html: `<div style="position:relative; width:3px; height:3px;">
             <div style="
               position:absolute; left:50%; top:-9px; transform:translateX(-50%);
               font-size:7px; font-weight:600; white-space:nowrap; color:#facc15; ${textShadow}
             ">${iata}</div>
             <div style="
               width:3px; height:3px; border-radius:50%;
               background:#facc15; border:1px solid #713f12;
               box-shadow:0 1px 2px rgba(0,0,0,0.6);
             "></div>
           </div>`,
    iconSize: [3, 3],
    iconAnchor: [1.5, 1.5],
    popupAnchor: [0, -5],
  });
}
