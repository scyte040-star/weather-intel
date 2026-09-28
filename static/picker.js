// Location picker shared by the dashboard and the shelter page: place search (via /api/places), GPS, or a map click.
(() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const perMap = new WeakMap();  // Leaflet map -> { layer for the chosen-spot marker, picker waiting for a click }

  function forMap(map) {
    let st = perMap.get(map);
    if (!st) {
      st = { layer: L.layerGroup().addTo(map), waiting: null };
      map.on("click", ev => {
        const p = st.waiting;
        if (!p) return;
        st.waiting = null;
        map.getContainer().classList.remove("picking");
        p.set({ lat: ev.latlng.lat, lon: ev.latlng.lng, label: `Pinned at ${ev.latlng.lat.toFixed(3)}, ${ev.latlng.lng.toFixed(3)}` });
      });
      perMap.set(map, st);
    }
    return st;
  }

  window.makePicker = (root, map) => {
    const st = forMap(map);
    root.innerHTML = `<div class="search"><input type="search" placeholder="Search a place in India" aria-label="Search a place">
        <button type="button" class="btn small ghost" data-way="search">Search</button></div>
      <ul class="results"></ul>
      <div class="ways"><button type="button" class="btn small ghost" data-way="gps">Use my location</button>
        <button type="button" class="btn small ghost" data-way="map">Pick on the map</button></div>
      <p class="chosen none">No location chosen yet</p>`;
    const picker = { value: null, root };
    const input = root.querySelector("input"), results = root.querySelector(".results"), chosen = root.querySelector(".chosen");
    const say = text => { chosen.className = "chosen none"; chosen.textContent = text; };

    // quiet: show a saved value without moving the map or dropping a marker
    picker.set = (v, quiet = false) => {
      picker.value = v;
      chosen.className = `chosen${v ? "" : " none"}`;
      chosen.textContent = v ? `📍 ${v.label}` : "No location chosen yet";
      results.innerHTML = "";
      if (quiet) return;
      st.layer.clearLayers();
      if (v) {
        L.marker([v.lat, v.lon], { icon: L.divIcon({ className: "", html: '<div class="pick-pin"></div>', iconSize: [18, 18] }) }).addTo(st.layer);
        map.flyTo([v.lat, v.lon], Math.max(map.getZoom(), 9), { duration: .6 });
      }
      root.dispatchEvent(new Event("change"));
    };
    picker.clearMarker = () => st.layer.clearLayers();

    async function search() {
      if (input.value.trim().length < 2) return say("Type at least 2 characters.");
      say("Searching…");
      try {
        const res = await fetch(`/api/places?q=${encodeURIComponent(input.value.trim())}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error);
        if (!data.places.length) return say("No places found. Try a nearby town, or pick on the map.");
        say("Choose one:");
        results.innerHTML = data.places.map((p, i) => `<li><button type="button" data-i="${i}">${esc(p.label)}</button></li>`).join("");
        results.onclick = ev => {
          const p = data.places[ev.target.closest("[data-i]")?.dataset.i];
          if (p) picker.set({ lat: p.lat, lon: p.lon, label: p.label });
        };
      } catch (err) {
        say(err.message || "Search failed.");
      }
    }
    input.addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); search(); } });
    root.addEventListener("click", ev => {
      const way = ev.target.closest("[data-way]")?.dataset.way;
      if (way === "search") search();
      if (way === "gps") {
        if (!navigator.geolocation) return say("This browser can't share its location.");
        say("Finding you…");
        navigator.geolocation.getCurrentPosition(
          pos => picker.set({ lat: pos.coords.latitude, lon: pos.coords.longitude, label: "My current location" }),
          () => say("Location access was blocked. Search for a place or pick on the map."), { timeout: 10000 });
      }
      if (way === "map") {
        st.waiting = picker;
        map.getContainer().classList.add("picking");
        say("Click the map to choose the spot.");
        map.getContainer().scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
    return picker;
  };
})();
