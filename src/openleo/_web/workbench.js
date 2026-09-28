/* Native, offline workbench. Scientific values are never interpolated. */
(() => {
  'use strict';
  const A = 6378137, B = 6356752.314245, RAD = Math.PI / 180;
  const ROUTES = [['minimum_delay', 'Minimum delay'], ['maximum_rate', 'Maximum rate'], ['fixed_capacity', 'Fixed-capacity baseline']];
  const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);
  function basis(camera) {
    const c = Math.cos(camera.lon), s = Math.sin(camera.lon), u = Math.sin(camera.lat), v = Math.cos(camera.lat);
    return [[-s, c, 0], [-u * c, -u * s, v], [v * c, v * s, u]];
  }
  function ecef(lon, lat) {
    const p = lat * RAD, l = lon * RAD, e2 = 1 - B * B / (A * A);
    const n = A / Math.sqrt(1 - e2 * Math.sin(p) ** 2);
    return [n * Math.cos(p) * Math.cos(l), n * Math.cos(p) * Math.sin(l), n * (1 - e2) * Math.sin(p)];
  }
  function project(point, camera) {
    const [east, north, front] = basis(camera);
    return [dot(point, east) / A || 0, -dot(point, north) / A || 0, dot(point, front) / A || 0];
  }
  function visible(point, camera) {
    const toward = basis(camera)[2], q = [point[0] / A, point[1] / A, point[2] / B];
    const ray = [toward[0], toward[1], toward[2] * A / B];
    const a = dot(ray, ray), b = 2 * dot(q, ray), c = dot(q, q) - 1, d = b * b - 4 * a * c;
    return d <= 0 || (-b + Math.sqrt(d)) / (2 * a) <= 1e-7;
  }
  // Bound visual subdivisions to 256; distant-orbit detail can use screen-space clipping.
  const segmentCount = (start, end) => Math.min(256, Math.max(1, Math.ceil(Math.hypot(...end.map((value, i) => value - start[i])) / (A * .02))));
  const getLink = (data, time, station, satellite) => (data.links[time] || []).find(link => link.station_index === station && link.satellite_index === satellite) || null;
  function initialSatellite(data, station) {
    const links = (data.links[0] || []).filter(link => link.station_index === station);
    if (links.length) return links.reduce((best, link) => link.rate_bps > best.rate_bps || (link.rate_bps === best.rate_bps && link.elevation_deg > best.elevation_deg) ? link : best).satellite_index;
    return data.links.flat().find(link => link.station_index === station)?.satellite_index || 0;
  }
  const series = (data, station, satellite, key) => data.timestamps_utc.map((_, i) => getLink(data, i, station, satellite)?.[key] ?? (key === 'rate_bps' ? 0 : null));
  const formatNumber = (value, digits = 2) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-GB', {maximumFractionDigits: digits}) : '—';
  function formatSI(value, unit) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    const scales = [[1e12, 'T'], [1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, '']];
    const [scale, prefix] = scales.find(([threshold]) => Math.abs(value) >= threshold) || scales[4];
    return formatNumber(value / scale) + ' ' + prefix + unit;
  }
  const embeddedJSON = value => JSON.stringify(value).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  function standaloneHTML(html, experiment) {
    for (const [id, value] of [['experiment-data', experiment], ['session-data', {live: false, token: ''}]]) {
      const pattern = new RegExp('(<script\\b[^>]*\\bid="' + id + '"[^>]*>)[\\s\\S]*?(<\\/script\\s*>)', 'i');
      if (!pattern.test(html)) throw new Error('Missing ' + id + ' in report');
      html = html.replace(pattern, (_, open, close) => open + embeddedJSON(value) + close);
    }
    return '<!doctype html>\n' + html.replace(/^<!doctype html>\s*/i, '');
  }
  function csvCell(value) {
    let text = value == null ? '' : String(value);
    if (typeof value === 'string' && /^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }
  const csv = rows => rows.map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
  const nodeName = (data, node) => node < data.satellites.length ? data.satellites[node]?.name : data.stations[node - data.satellites.length]?.name;
  const HYDRO_FIELDS = ['cloud_attenuation_db', 'rain_specific_attenuation_db_per_km', 'rain_path_length_m', 'rain_attenuation_db', 'hydrometeor_attenuation_db', 'total_atmospheric_attenuation_db'];
  const dryLayer = height => ({liquid_water_kg_m2: 0, rain_rate_mm_h: 0, rain_top_height_amsl_m: height, polarization_tilt_deg: 0});
  function changeStation(value, selected, action, name) {
    const oldName = value.stations[selected]?.name;
    if (!oldName) throw new Error('Select a station.');
    if (action === 'rename') {
      name = String(name || '').trim();
      if (!name) throw new Error('Enter a station name.');
      if (value.stations.some((station, i) => i !== selected && station.name === name)) throw new Error('Station names must be unique.');
    } else if (action === 'add') {
      if (value.stations.length >= 16) throw new Error('At most 16 stations are supported.');
      let n = 1; while (value.stations.some(station => station.name === 'Station ' + n)) n++;
      name = 'Station ' + n;
    } else if (action === 'remove') {
      if (value.stations.length <= (value.network ? 2 : 1)) throw new Error(value.network ? 'Network scenarios need at least two stations.' : 'Keep at least one station.');
    } else throw new Error('Unknown station edit.');
    const stations = action === 'add' ? [...value.stations, {name, latitude_deg: 0, longitude_deg: 0, height_m: 0}] : action === 'remove' ? value.stations.filter((_, i) => i !== selected) : value.stations.map((station, i) => i === selected ? {...station, name} : station);
    const remap = (map, fallback) => Object.fromEntries(stations.map(station => [station.name, action === 'rename' && station.name === name ? map[oldName] : map[station.name] ?? fallback(station.name)]));
    const propagation = value.propagation && {...value.propagation, station_heights_amsl_m: remap(value.propagation.station_heights_amsl_m, () => 0)};
    if (propagation?.hydrometeors) propagation.hydrometeors = {...propagation.hydrometeors, stations: remap(propagation.hydrometeors.stations, station => dryLayer(propagation.station_heights_amsl_m[station]))};
    const network = value.network && {...value.network};
    if (network) for (const [key, other] of [['source_station', 'target_station'], ['target_station', 'source_station']]) {
      if (network[key] === oldName && action === 'rename') network[key] = name;
      if (network[key] === oldName && action === 'remove') network[key] = stations.find(station => station.name !== network[other]).name;
    }
    return {...value, stations, ...(propagation ? {propagation} : {}), ...(network ? {network} : {})};
  }
  function changeAtmosphere(value, selected, key, number) {
    if (key === 'gas' && !number) { const {propagation, ...rest} = value; return rest; }
    const name = value.stations[selected].name;
    let propagation = value.propagation || {model: 'itu_reference', refinement: 1, station_heights_amsl_m: Object.fromEntries(value.stations.map(station => [station.name, 0]))};
    if (key === 'hydrometeors') {
      const {hydrometeors, ...gas} = propagation;
      propagation = number ? {...gas, hydrometeors: hydrometeors || {model: 'declared_uniform_layers', stations: Object.fromEntries(value.stations.map(station => [station.name, dryLayer(propagation.station_heights_amsl_m[station.name])]))}} : gas;
    } else if (key !== 'gas') {
      if (!Number.isFinite(number)) throw new Error('Enter a finite atmospheric value.');
      if (key === 'amsl') {
        if (number < 0 || number > 10000) throw new Error('AMSL height must be in 0..10000 m.');
        if (propagation.hydrometeors && number > propagation.hydrometeors.stations[name].rain_top_height_amsl_m) throw new Error('Raise the declared rain top before raising station AMSL.');
        propagation = {...propagation, station_heights_amsl_m: {...propagation.station_heights_amsl_m, [name]: number}};
      } else if (key === 'refinement') {
        if (![1, 2, 4].includes(number)) throw new Error('Refinement must be 1, 2 or 4.');
        propagation = {...propagation, refinement: number};
      } else {
        if (key === 'rain_top_height_amsl_m' && (number < propagation.station_heights_amsl_m[name] || number > 20000)) throw new Error('Rain top must be between station AMSL and 20000 m.');
        if (number < 0 || (key === 'polarization_tilt_deg' && number > 180)) throw new Error('Atmospheric values are outside the declared bounds.');
        if (!propagation.hydrometeors) throw new Error('Enable declared hydrometeor layers first.');
        propagation = {...propagation, hydrometeors: {...propagation.hydrometeors, stations: {...propagation.hydrometeors.stations, [name]: {...propagation.hydrometeors.stations[name], [key]: number}}}};
      }
    }
    return {...value, propagation};
  }
  function studyLinks(result) {
    const match = /^\/api\/study\/([a-f0-9]{64})\/index\.html$/.exec(result.report_url);
    if (!match) throw new Error('The local process returned an invalid report URL.');
    if (result.archive_url !== '/api/study/' + match[1] + '/archive.zip') throw new Error('The local process returned an invalid archive URL.');
    return {report_url: result.report_url, archive_url: result.archive_url};
  }
  function comparisonWindow(scenario, startOffset = 0, duration) {
    const window = scenario.time_window;
    const micros = value => Date.parse(value) * 1000 + Number((value.match(/\.(\d+)Z$/)?.[1] || '').padEnd(6, '0').slice(3, 6));
    const span = (micros(window.stop_utc) - micros(window.start_utc)) / 1e6, minimum = window.step_s;
    const maximum = Math.min(3600, Math.max(0, span - startOffset));
    duration = duration ?? Math.min(Math.max(60, minimum), maximum);
    return {minimum, maximum, duration, maximumOffset: Math.max(0, span - duration), supported: minimum <= Math.min(3600, span), valid: startOffset >= 0 && duration >= minimum && duration <= maximum};
  }
  function linksCSV(data) {
    const propagation = ['2', '3'].includes(data.schema_version) ? ['gaseous_dry_attenuation_db', 'gaseous_water_attenuation_db', 'gaseous_attenuation_db', 'free_space_cn0_db_hz', 'geometric_delay_s', 'atmospheric_excess_delay_s', 'apparent_elevation_deg', ...(data.schema_version === '3' ? HYDRO_FIELDS : [])] : [];
    const fields = ['station_index', 'satellite_index', 'elevation_deg', 'azimuth_deg', 'range_m', 'range_rate_mps', 'doppler_hz', 'delay_s', 'cn0_db_hz', 'snr_db', 'esn0_db', 'modcod', 'rate_bps', 'shannon_upper_bound_bps', 'margin_db', 'remaining_contact_s', 'contact_truncated', ...propagation];
    return csv([['timestamp_utc', 'station', 'satellite', 'norad_id', ...fields], ...data.links.flatMap((links, time) => links.map(link => [data.timestamps_utc[time], data.stations[link.station_index].name, data.satellites[link.satellite_index].name, data.satellites[link.satellite_index].norad_id, ...fields.map(key => link[key])]))]);
  }
  function routesCSV(data) {
    return csv([['timestamp_utc', 'model', 'connected', 'delay_s', 'bottleneck_bps', 'path'], ...data.timestamps_utc.flatMap((time, i) => ROUTES.map(([key]) => {
      const route = data.network?.frames[i]?.[key];
      return [time, key, !!route, route?.delay_s, route?.bottleneck_bps, route?.nodes.map(node => nodeName(data, node)).join(' → ')];
    }))]);
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {ecef, project, visible, segmentCount, getLink, initialSatellite, series, formatNumber, formatSI, embeddedJSON, standaloneHTML, linksCSV, routesCSV, changeStation, changeAtmosphere, studyLinks, comparisonWindow};
  if (typeof document === 'undefined') return;
  const el = id => document.getElementById(id);
  const text = (id, value) => { el(id).textContent = value; };
  const read = id => JSON.parse(el(id).textContent);
  const metric = (value, unit, digits = 2) => value == null ? '—' : formatNumber(value, digits) + unit;
  const utc = value => value.replace('T', ' · ').replace('Z', ' UTC');
  const timeOnly = value => value.split('T')[1].replace('Z', '');
  const percent = value => metric(value == null ? null : value * 100, '%', 1);
  let data, coast, session;
  try {
    data = read('experiment-data'); coast = read('coastline-data'); session = read('session-data');
    if (!data.timestamps_utc?.length || !data.satellites?.length || !data.stations?.length) throw new Error('The experiment has no samples, satellites or stations.');
  } catch (error) { text('startup-error', 'Unable to open this experiment: ' + error.message); el('startup-error').hidden = false; return; }
  let state = {time: 0, station: 0, satellite: 0, route: 'minimum_delay', zoom: 1, lon: 0, lat: 0};
  let playing = false, timer = null, noticeTimer = null, drag = null;
  const globe = el('globe'), context = globe.getContext('2d');
  const grid = [];
  for (let lat = -60; lat <= 60; lat += 30) grid.push(Array.from({length: 121}, (_, i) => ecef(i * 3 - 180, lat)));
  for (let lon = -180; lon < 180; lon += 30) grid.push(Array.from({length: 61}, (_, i) => ecef(lon, i * 3 - 90)));
  const coastlines = (coast.features || []).flatMap(feature => feature.geometry.type === 'LineString' ? [feature.geometry.coordinates] : feature.geometry.type === 'MultiLineString' ? feature.geometry.coordinates : []).map(line => line.map(([lon, lat]) => ecef(lon, lat)));
  function announce(message) {
    clearTimeout(noticeTimer); text('announcement', message); el('announcement').hidden = false;
    noticeTimer = setTimeout(() => { el('announcement').hidden = true; }, 5000);
  }
  function options(id, values, chosen) {
    el(id).replaceChildren(...values.map((name, i) => {
      const option = document.createElement('option'); option.value = i; option.textContent = name; return option;
    }));
    el(id).value = chosen;
  }
  function focusStation(reset = false) {
    const station = data.stations[state.station];
    state = {...state, lon: station.longitude_deg * RAD, lat: station.latitude_deg * RAD, zoom: reset ? 1 : state.zoom};
    drawGlobe();
  }
  function refreshExperiment() {
    state = {...state, time: Math.min(state.time, data.timestamps_utc.length - 1), station: Math.min(state.station, data.stations.length - 1), satellite: Math.min(state.satellite, data.satellites.length - 1)};
    text('experiment-name', data.scenario.name); document.title = data.scenario.name + ' · OpenLEO';
    text('app-version', data.provenance?.software?.['openleo-link'] || 'Workbench');
    options('station-select', data.stations.map(station => station.name), state.station);
    options('satellite-select', data.satellites.map(satellite => satellite.name + ' · ' + satellite.norad_id), state.satellite);
    text('satellite-count', data.satellites.length); text('station-count', data.stations.length);
    const minutes = (Date.parse(data.timestamps_utc.at(-1)) - Date.parse(data.timestamps_utc[0])) / 60000;
    text('window-duration', minutes >= 60 ? formatNumber(minutes / 60) + ' h' : formatNumber(minutes) + ' min');
    text('sample-count', data.timestamps_utc.length + ' discrete samples');
    text('start-time', timeOnly(data.timestamps_utc[0])); text('stop-time', timeOnly(data.timestamps_utc.at(-1)));
    el('timeline').max = data.timestamps_utc.length - 1;
    text('session-badge', session.live ? 'LOCAL SESSION' : 'OFFLINE REPORT');
    text('session-note', session.live ? 'Edit locally · export a portable report' : 'Self-contained · no server connection');
    text('editor-mode', session.live ? 'Changes are computed by your local OpenLEO process using the pinned orbital catalog.' : 'This is an offline report. Editing is disabled. Start openleo app with your scenario to recompute; exploration and exports work here.');
    el('quick-fields').disabled = !session.live; el('scenario-json').readOnly = !session.live;
    el('run-scenario').disabled = !session.live; el('reset-scenario').disabled = !session.live;
    renderMetadata(); renderNetworkRows(); render();
  }
  function renderMetadata() {
    for (const [id, values] of [['warnings-list', data.warnings], ['limitations-list', data.limitations]]) {
      el(id).replaceChildren(...(values?.length ? values : ['None recorded.']).map(value => {
        const li = document.createElement('li'); li.textContent = typeof value === 'string' ? value : JSON.stringify(value); return li;
      }));
    }
    for (const [id, value] of [['models-json', data.models], ['provenance-json', data.provenance], ['epochs-json', data.satellites.map(({name, norad_id, epoch_utc, maximum_absolute_element_age_days}) => ({name, norad_id, epoch_utc, maximum_absolute_element_age_days}))]]) text(id, JSON.stringify(value, null, 2));
  }
  function renderNetworkRows() {
    const rows = ROUTES.map(([key, label]) => {
      const row = document.createElement('tr'); row.id = 'route-' + key;
      const title = document.createElement('td'), choice = document.createElement('label'), radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'route-model'; radio.value = key; radio.checked = state.route === key;
      radio.addEventListener('change', () => { state = {...state, route: key}; render(); });
      choice.append(radio, document.createTextNode(label)); title.append(choice); row.append(title);
      for (let i = 0; i < 6; i++) row.append(document.createElement('td'));
      return row;
    });
    el('network-rows').replaceChildren(...rows);
  }
  function renderNetwork() {
    const network = data.network, frame = network?.frames[state.time];
    const source = data.stations[network?.source_station_index], target = data.stations[network?.target_station_index];
    text('network-endpoints', source && target ? source.name + ' → ' + target.name : 'No network result');
    ROUTES.forEach(([key]) => {
      const route = frame?.[key], summary = network?.summary?.[key], row = el('route-' + key);
      row.classList.toggle('selected-route', state.route === key);
      const values = [route ? route.nodes.length - 1 + ' hops' : 'No path', metric(route ? route.delay_s * 1000 : null, ' ms'), formatSI(route?.bottleneck_bps, 'bit/s'), percent(summary?.connected_sample_fraction), formatSI(summary?.integrated_bottleneck_bits, 'bit'), formatNumber(summary?.route_changes, 0)];
      values.forEach((value, i) => { row.cells[i + 1].textContent = value; });
    });
    const selected = frame?.[state.route];
    text('route-path', selected ? selected.nodes.map(node => nodeName(data, node)).join(' → ') : 'No connected path at this sample under the selected model.');
  }
  function renderTelemetry() {
    const link = getLink(data, state.time, state.station, state.satellite), station = data.stations[state.station];
    const usable = link?.rate_bps > 0;
    text('selected-satellite', data.satellites[state.satellite].name); text('selected-station', station.name);
    text('link-status', usable ? 'RF USABLE' : link ? 'RF OUTAGE' : 'BELOW MASK');
    el('link-status').className = 'badge' + (usable ? '' : ' outage');
    text('link-rate', formatSI(link?.rate_bps ?? 0, 'bit/s'));
    text('link-modcod', link?.modcod || 'No RF lock'); text('link-margin', link?.margin_db == null ? '— margin' : metric(link.margin_db, ' dB margin'));
    const values = {'link-elevation': metric(link?.elevation_deg, '°', 1), 'link-range': metric(link ? link.range_m / 1000 : null, ' km', 1), 'link-doppler': formatSI(link?.doppler_hz, 'Hz'), 'link-delay': metric(link ? link.delay_s * 1000 : null, ' ms'), 'link-cn0': metric(link?.cn0_db_hz, ' dBHz', 1), 'link-esn0': metric(link?.esn0_db, ' dB', 1), 'link-azimuth': metric(link?.azimuth_deg, '°', 1), 'link-contact': link ? (link.contact_truncated ? '≥ ' : '') + metric(link.remaining_contact_s, ' s', 0) : '—'};
    Object.entries(values).forEach(([id, value]) => text(id, value));
    text('propagation-model', data.schema_version === '3' ? 'ITU gas + declared cloud/rain' : data.schema_version === '2' ? 'ITU reference atmosphere' : 'Free space');
    el('propagation-values').hidden = !['2', '3'].includes(data.schema_version);
    el('hydrometeor-values').hidden = data.schema_version !== '3';
    text('link-gas-loss', metric(link?.gaseous_attenuation_db, ' dB', 3));
    text('link-apparent-elevation', metric(link?.apparent_elevation_deg, '°', 3));
    text('link-excess-delay', metric(link?.atmospheric_excess_delay_s == null ? null : link.atmospheric_excess_delay_s * 1e6, ' µs', 3));
    for (const [id, key, unit] of [['link-cloud-loss', HYDRO_FIELDS[0], ' dB'], ['link-rain-specific', HYDRO_FIELDS[1], ' dB/km'], ['link-rain-path', HYDRO_FIELDS[2], ' m'], ['link-rain-loss', HYDRO_FIELDS[3], ' dB'], ['link-hydrometeor-loss', HYDRO_FIELDS[4], ' dB'], ['link-total-loss', HYDRO_FIELDS[5], ' dB']]) text(id, metric(link?.[key], unit, 3));
    el('link-contact').title = link?.contact_truncated ? 'Contact continues beyond this experiment window; duration is a sampled lower bound.' : 'Sampled look-ahead to the visibility mask crossing.';
    const stats = data.statistics?.find(entry => entry.station_index === state.station);
    text('station-visible', percent(stats?.visible_sample_fraction)); text('station-usable', percent(stats?.usable_sample_fraction));
    text('station-bits', formatSI(stats?.best_link_integrated_bits, 'bit')); text('station-fixed-bits', formatSI(stats?.fixed_baseline_integrated_bits, 'bit')); text('station-handovers', formatNumber(stats?.handover_count, 0));
    text('chart-context', data.satellites[state.satellite].name + ' → ' + station.name + ' · exact sample values');
    text('rate-chart-value', formatSI(link?.rate_bps ?? 0, 'bit/s')); text('elevation-chart-value', values['link-elevation']); text('doppler-chart-value', values['link-doppler']);
  }
  function render() {
    el('timeline').value = state.time; el('timeline').setAttribute('aria-valuetext', utc(data.timestamps_utc[state.time]));
    text('current-time', utc(data.timestamps_utc[state.time])); el('current-time').dateTime = data.timestamps_utc[state.time];
    text('status-time', utc(data.timestamps_utc[state.time])); el('status-time').dateTime = data.timestamps_utc[state.time];
    text('sample-position', state.time + 1 + ' / ' + data.timestamps_utc.length);
    text('visible-count', data.links[state.time].length); text('usable-count', data.links[state.time].filter(link => link.rate_bps > 0).length + ' RF-usable at this sample');
    el('previous-sample').disabled = state.time === 0; el('next-sample').disabled = state.time === data.timestamps_utc.length - 1;
    renderTelemetry(); renderNetwork(); drawGlobe(); drawCharts();
  }
  function sizeCanvas(canvas) {
    const width = canvas.clientWidth, height = canvas.clientHeight, ratio = Math.min(devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    return {ctx, width, height};
  }
  function drawGlobe() {
    const {width, height} = sizeCanvas(globe); if (!width || !height) return;
    const radius = Math.min(width, height) * .347 * state.zoom, cx = width / 2, cy = height / 2 + 2;
    const screen = point => { const [x, y, z] = project(point, state); return [cx + x * radius, cy + y * radius, z]; };
    const ry = radius * Math.sqrt(Math.sin(state.lat) ** 2 + (B / A * Math.cos(state.lat)) ** 2);
    context.fillStyle = '#edf3f6'; context.strokeStyle = '#9caeb9'; context.lineWidth = 1;
    context.beginPath(); context.ellipse(cx, cy, radius, ry, 0, 0, Math.PI * 2); context.fill(); context.stroke();
    function path(points, color, lineWidth = 1, densify = false) {
      context.beginPath(); context.strokeStyle = color; context.lineWidth = lineWidth; let active = false;
      for (let i = 0; i < points.length; i++) {
        const previous = points[Math.max(0, i - 1)], next = points[i];
        const pieces = densify ? segmentCount(previous, next) : 1;
        for (let part = 1; part <= pieces; part++) {
          const point = pieces === 1 ? next : next.map((v, j) => previous[j] + (v - previous[j]) * part / pieces);
          if (!visible(point, state)) { active = false; continue; }
          const [x, y] = screen(point); if (active) context.lineTo(x, y); else context.moveTo(x, y); active = true;
        }
      }
      context.stroke();
    }
    grid.forEach(line => path(line, '#bac8d0', .6));
    coastlines.forEach(line => path(line, '#6f8793', .85));
    const positions = data.satellites.map(satellite => satellite.positions_ecef_m[state.time]);
    const position = node => node < positions.length ? positions[node] : data.stations[node - positions.length]?.position_ecef_m;
    const frame = data.network?.frames[state.time];
    if (el('show-isl').checked) (frame?.isl_edges || []).forEach(([a, b]) => path([positions[a], positions[b]], '#718a9d40', .55, true));
    if (el('show-ground').checked) data.links[state.time].forEach(link => path([data.stations[link.station_index].position_ecef_m, positions[link.satellite_index]], link.station_index === state.station ? '#b6632280' : '#718a9d40', .7, true));
    const track = data.satellites[state.satellite].positions_ecef_m;
    path(track, '#60788c', 1.05, true);
    const route = frame?.[state.route];
    if (route) path(route.nodes.map(position), '#267544', 1.8, true);
    function marker(point, color, radiusPx, ring = false) {
      if (!visible(point, state)) return;
      const [x, y] = screen(point); context.fillStyle = color; context.beginPath(); context.arc(x, y, radiusPx, 0, Math.PI * 2); context.fill();
      if (ring) { context.strokeStyle = color; context.lineWidth = 1; context.beginPath(); context.arc(x, y, radiusPx + 4, 0, Math.PI * 2); context.stroke(); }
    }
    positions.forEach((point, i) => marker(point, i === state.satellite ? '#004777' : '#006bb6', i === state.satellite ? 3.8 : 2.2, i === state.satellite));
    data.stations.forEach((station, i) => {
      marker(station.position_ecef_m, '#ae5200', i === state.station ? 3.6 : 2.8, i === state.station);
      if (visible(station.position_ecef_m, state)) { const [x, y] = screen(station.position_ecef_m); context.font = '11px "Segoe UI", sans-serif'; context.fillStyle = '#884000'; context.fillText(station.name, x + 10, y - 7); }
    });
    text('camera-position', formatNumber(state.lat / RAD, 1) + '° N / ' + formatNumber(state.lon / RAD, 1) + '° E · ' + formatNumber(state.zoom, 1) + '×');
  }
  function drawChart(id, key, color, unit, divisor = 1) {
    const canvas = el(id), {ctx, width, height} = sizeCanvas(canvas); if (!width || !height) return;
    const values = series(data, state.station, state.satellite, key).map(value => value == null ? null : value / divisor);
    const finite = values.filter(value => value != null), min = Math.min(0, ...finite), max = Math.max(1, ...finite);
    const left = 44, right = width - 8, top = 12, bottom = height - 25;
    const t0 = Date.parse(data.timestamps_utc[0]), duration = Date.parse(data.timestamps_utc.at(-1)) - t0 || 1;
    const x = i => left + (Date.parse(data.timestamps_utc[i]) - t0) / duration * (right - left);
    const y = value => bottom - (value - min) / (max - min) * (bottom - top);
    ctx.font = '10px ui-monospace, monospace'; ctx.fillStyle = '#596570'; ctx.lineWidth = .6;
    for (let i = 0; i <= 2; i++) {
      const value = min + (max - min) * i / 2, position = y(value); ctx.strokeStyle = '#dbe0e4'; ctx.beginPath(); ctx.moveTo(left, position); ctx.lineTo(right, position); ctx.stroke();
      ctx.fillText(formatNumber(value, 1), 1, position + 3);
    }
    ctx.fillText(unit, 1, height - 3); ctx.fillText(timeOnly(data.timestamps_utc[0]).slice(0, 5), left, height - 3);
    ctx.textAlign = 'right'; ctx.fillText(timeOnly(data.timestamps_utc.at(-1)).slice(0, 5) + ' UTC', right, height - 3); ctx.textAlign = 'left';
    ctx.strokeStyle = '#99a4ae'; ctx.beginPath(); ctx.moveTo(left, top); ctx.lineTo(left, bottom); ctx.lineTo(right, bottom); ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath(); let active = false;
    values.forEach((value, i) => {
      if (value == null) { active = false; return; }
      if (active) { if (key === 'rate_bps') ctx.lineTo(x(i), y(values[i - 1])); ctx.lineTo(x(i), y(value)); } else ctx.moveTo(x(i), y(value)); active = true;
    }); ctx.stroke();
    ctx.fillStyle = color; values.forEach((value, i) => { if (value != null) { ctx.beginPath(); ctx.arc(x(i), y(value), 1.4, 0, Math.PI * 2); ctx.fill(); } });
    ctx.strokeStyle = '#65727e'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x(state.time), top); ctx.lineTo(x(state.time), bottom); ctx.stroke(); ctx.setLineDash([]);
    if (values[state.time] != null) { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x(state.time), y(values[state.time]), 3, 0, Math.PI * 2); ctx.fill(); }
    if (!finite.length) { ctx.fillStyle = '#596570'; ctx.textAlign = 'center'; ctx.fillText('No above-mask samples for this link', (left + right) / 2, height / 2); ctx.textAlign = 'left'; }
  }
  function drawCharts() {
    drawChart('rate-chart', 'rate_bps', '#006bb6', 'Mbit/s', 1e6);
    drawChart('elevation-chart', 'elevation_deg', '#ae5200', '°');
    drawChart('doppler-chart', 'doppler_hz', '#267544', 'kHz', 1e3);
  }
  function setTime(value) { state = {...state, time: Math.max(0, Math.min(data.timestamps_utc.length - 1, value))}; render(); }
  function pause() {
    playing = false; clearInterval(timer); timer = null; text('play-pause', '▶'); el('play-pause').setAttribute('aria-label', 'Play timeline'); el('play-pause').setAttribute('aria-pressed', 'false');
  }
  function play() {
    pause(); if (state.time === data.timestamps_utc.length - 1) setTime(0);
    playing = true; text('play-pause', 'Ⅱ'); el('play-pause').setAttribute('aria-label', 'Pause timeline'); el('play-pause').setAttribute('aria-pressed', 'true');
    timer = setInterval(() => { if (state.time >= data.timestamps_utc.length - 1) pause(); else setTime(state.time + 1); }, 1000 / Number(el('playback-speed').value));
  }
  function zoom(factor) { state = {...state, zoom: Math.max(.65, Math.min(2.4, state.zoom * factor))}; drawGlobe(); }
  function rotate(lon, lat) {
    state = {...state, lon: ((lon + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI, lat: Math.max(-Math.PI / 2, Math.min(Math.PI / 2, lat))}; drawGlobe();
  }
  el('timeline').addEventListener('input', event => { pause(); setTime(Number(event.target.value)); });
  el('previous-sample').addEventListener('click', () => { pause(); setTime(state.time - 1); });
  el('next-sample').addEventListener('click', () => { pause(); setTime(state.time + 1); });
  el('play-pause').addEventListener('click', () => playing ? pause() : play());
  el('playback-speed').addEventListener('change', () => { if (playing) play(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
  el('station-select').addEventListener('change', event => { state = {...state, station: Number(event.target.value)}; render(); });
  el('satellite-select').addEventListener('change', event => { state = {...state, satellite: Number(event.target.value)}; render(); });
  el('focus-station').addEventListener('click', () => focusStation()); el('reset-view').addEventListener('click', () => focusStation(true));
  el('zoom-in').addEventListener('click', () => zoom(1.15)); el('zoom-out').addEventListener('click', () => zoom(1 / 1.15));
  for (const id of ['show-ground', 'show-isl']) el(id).addEventListener('change', drawGlobe);
  globe.addEventListener('wheel', event => { event.preventDefault(); zoom(Math.exp(-event.deltaY * .001)); }, {passive: false});
  globe.addEventListener('pointerdown', event => { globe.setPointerCapture(event.pointerId); drag = {x: event.clientX, y: event.clientY, lon: state.lon, lat: state.lat, moved: false}; });
  globe.addEventListener('pointermove', event => {
    if (!drag) return; const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    drag = {...drag, moved: drag.moved || Math.abs(dx) + Math.abs(dy) > 4}; rotate(drag.lon - dx * .006, drag.lat + dy * .006);
  });
  globe.addEventListener('pointerup', event => {
    if (drag && !drag.moved) {
      const bounds = globe.getBoundingClientRect(), radius = Math.min(bounds.width, bounds.height) * .347 * state.zoom;
      const hit = data.satellites.map((satellite, i) => {
        const point = satellite.positions_ecef_m[state.time], [x, y] = project(point, state);
        return {i, distance: visible(point, state) ? Math.hypot(bounds.width / 2 + x * radius - (event.clientX - bounds.left), bounds.height / 2 + 2 + y * radius - (event.clientY - bounds.top)) : Infinity};
      }).filter(item => item.distance < 14).sort((a, b) => a.distance - b.distance)[0];
      if (hit) { state = {...state, satellite: hit.i}; el('satellite-select').value = hit.i; render(); }
    }
    drag = null;
  });
  globe.addEventListener('pointercancel', () => { drag = null; });
  globe.addEventListener('keydown', event => {
    const moves = {ArrowLeft: [-.1, 0], ArrowRight: [.1, 0], ArrowUp: [0, .1], ArrowDown: [0, -.1]};
    if (moves[event.key]) { event.preventDefault(); rotate(state.lon + moves[event.key][0], state.lat + moves[event.key][1]); }
    else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(1.15); }
    else if (event.key === '-') { event.preventDefault(); zoom(1 / 1.15); }
    else if (event.key === 'Home') { event.preventDefault(); focusStation(true); }
  });
  for (const id of ['rate-chart', 'elevation-chart', 'doppler-chart']) el(id).addEventListener('click', event => {
    const bounds = event.currentTarget.getBoundingClientRect(), fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left - 44) / (bounds.width - 52)));
    const wanted = Date.parse(data.timestamps_utc[0]) + fraction * (Date.parse(data.timestamps_utc.at(-1)) - Date.parse(data.timestamps_utc[0]));
    const times = data.timestamps_utc.map(Date.parse); let index = 0;
    times.forEach((value, i) => { if (Math.abs(value - wanted) < Math.abs(times[index] - wanted)) index = i; }); pause(); setTime(index);
  });
  function download(content, filename, type) {
    const url = URL.createObjectURL(new Blob([content], {type})), anchor = document.createElement('a');
    anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000); announce('Exported ' + filename);
  }
  el('download-json').addEventListener('click', () => download(JSON.stringify(data, null, 2) + '\n', 'experiment.json', 'application/json'));
  el('download-links').addEventListener('click', () => download(linksCSV(data), 'links.csv', 'text/csv;charset=utf-8'));
  el('download-routes').addEventListener('click', () => download(routesCSV(data), 'routes.csv', 'text/csv;charset=utf-8'));
  el('download-report').addEventListener('click', () => {
    const root = document.documentElement.cloneNode(true);
    root.querySelectorAll('dialog[open]').forEach(dialog => dialog.removeAttribute('open'));
    root.querySelector('#announcement').hidden = true;
    download(standaloneHTML(root.outerHTML, data), 'explorer.html', 'text/html;charset=utf-8');
  });
  const fields = [['edit-latitude', 'stations', 'latitude_deg', 1], ['edit-longitude', 'stations', 'longitude_deg', 1], ['edit-height', 'stations', 'height_m', 1], ['edit-mask', 'time_window', 'minimum_elevation_deg', 1], ['edit-frequency', 'radio_link', 'carrier_frequency_hz', 1e9], ['edit-bandwidth', 'radio_link', 'channel_bandwidth_hz', 1e6], ['edit-eirp', 'radio_link', 'eirp_dbw', 1], ['edit-gain', 'radio_link', 'receiver_gain_dbi', 1], ['edit-symbols', 'adaptation', 'symbol_rate_baud', 1e6], ['edit-margin', 'adaptation', 'implementation_margin_db', 1]];
  function editorError(error) { text('editor-error', error ? error.message : ''); el('editor-error').hidden = !error; }
  function draft() {
    const value = JSON.parse(el('scenario-json').value);
    if (!value || typeof value !== 'object' || !Array.isArray(value.stations) || !value.stations.length) throw new Error('The scenario must contain a non-empty stations array.');
    return value;
  }
  function refreshFields() {
    try {
      const value = draft(), selected = Math.min(Number(el('editor-station').value) || 0, value.stations.length - 1);
      options('editor-station', value.stations.map(station => station.name), selected);
      fields.forEach(([id, group, key, scale]) => { const source = group === 'stations' ? value.stations[selected] : value[group]; el(id).value = source?.[key] == null ? '' : source[key] / scale; });
      const name = value.stations[selected].name, propagation = value.propagation, layer = propagation?.hydrometeors?.stations[name];
      el('edit-station-name').value = name;
      el('remove-station').disabled = !session.live || value.stations.length <= (value.network ? 2 : 1);
      el('add-station').disabled = !session.live || value.stations.length >= 16;
      el('edit-gas').value = propagation ? 'itu_reference' : 'none';
      el('edit-refinement').value = propagation?.refinement || 1;
      el('edit-amsl').value = propagation?.station_heights_amsl_m[name] ?? '';
      el('edit-hydrometeors').checked = !!propagation?.hydrometeors;
      for (const id of ['edit-refinement', 'edit-amsl', 'edit-hydrometeors']) el(id).disabled = !session.live || !propagation;
      el('edit-amsl').required = !!propagation;
      for (const [id, key] of hydroInputs) { el(id).value = layer?.[key] ?? ''; el(id).disabled = !session.live || !layer; el(id).required = !!layer; }
      el('edit-rain-top').min = propagation?.station_heights_amsl_m[name] ?? 0;
      el('edit-frequency').min = propagation ? 1 : .000001;
      el('edit-frequency').max = layer ? 200 : propagation ? 1000 : '';
      el('edit-mask').min = propagation ? 5 : 0;
      el('station-preset').value = ''; editorError(null);
    } catch (error) { editorError(error); }
  }
  function resetEditor() { el('scenario-json').value = JSON.stringify(data.scenario, null, 2); refreshFields(); }
  const hydroInputs = [['edit-cloud', 'liquid_water_kg_m2'], ['edit-rain', 'rain_rate_mm_h'], ['edit-rain-top', 'rain_top_height_amsl_m'], ['edit-tilt', 'polarization_tilt_deg']];
  function saveDraft(value) { el('scenario-json').value = JSON.stringify(value, null, 2); refreshFields(); }
  for (const [id, action] of [['edit-station-name', 'rename'], ['add-station', 'add'], ['remove-station', 'remove']]) el(id).addEventListener(action === 'rename' ? 'change' : 'click', () => {
    if (!session.live || (action === 'rename' && !el(id).reportValidity())) return;
    try {
      const selected = Number(el('editor-station').value), updated = changeStation(draft(), selected, action, el(id).value);
      // The new option does not exist until the select is rebuilt.
      if (action === 'add') options('editor-station', updated.stations.map(station => station.name), updated.stations.length - 1);
      saveDraft(updated);
    } catch (error) { editorError(error); }
  });
  for (const [id, key] of [['edit-gas', 'gas'], ['edit-hydrometeors', 'hydrometeors'], ['edit-amsl', 'amsl'], ['edit-refinement', 'refinement'], ...hydroInputs]) el(id).addEventListener('change', () => {
    if (!session.live || !el(id).reportValidity()) return;
    try {
      const number = key === 'gas' ? el(id).value === 'itu_reference' : key === 'hydrometeors' ? el(id).checked : Number(el(id).value);
      saveDraft(changeAtmosphere(draft(), Number(el('editor-station').value), key, number));
    } catch (error) { editorError(error); }
  });
  el('scenario-open').addEventListener('click', () => { pause(); resetEditor(); el('scenario-dialog').showModal(); });
  el('provenance-open').addEventListener('click', () => { pause(); el('provenance-dialog').showModal(); });
  document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  el('reset-scenario').addEventListener('click', resetEditor); el('scenario-json').addEventListener('change', refreshFields); el('editor-station').addEventListener('change', refreshFields);
  fields.forEach(([id, group, key, scale]) => el(id).addEventListener('change', () => {
    if (!session.live || !el(id).reportValidity()) return;
    try {
      const value = draft(), number = Number(el(id).value) * scale, selected = Number(el('editor-station').value);
      if (!Number.isFinite(number)) throw new Error('Enter a finite numeric value.');
      const updated = group === 'stations' ? {...value, stations: value.stations.map((station, i) => i === selected ? {...station, [key]: number} : station)} : {...value, [group]: {...value[group], [key]: number}};
      el('scenario-json').value = JSON.stringify(updated, null, 2); editorError(null);
    } catch (error) { editorError(error); }
  }));
  el('station-preset').addEventListener('change', event => {
    const presets = {madrid: [40.4168, -3.7038, 650], tromso: [69.6492, 18.9553, 0], singapore: [1.3521, 103.8198, 0], quito: [-.1807, -78.4678, 2850]};
    if (!session.live || !presets[event.target.value]) return;
    try {
      const value = draft(), [latitude_deg, longitude_deg, height_m] = presets[event.target.value], selected = Number(el('editor-station').value);
      const updated = {...value, stations: value.stations.map((station, i) => i === selected ? {...station, latitude_deg, longitude_deg, height_m} : station)};
      el('scenario-json').value = JSON.stringify(updated, null, 2); refreshFields();
    } catch (error) { editorError(error); }
  });
  el('scenario-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!session.live) return;
    el('run-scenario').disabled = true; text('run-scenario', 'Computing…'); editorError(null);
    try {
      const response = await fetch('/api/simulate', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-OpenLEO-Token': session.token}, body: JSON.stringify({scenario: draft()})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || 'The local simulation failed.');
      if (!result.timestamps_utc?.length || !result.satellites?.length || !result.stations?.length) throw new Error('The local process returned an incomplete experiment.');
      data = result; el('experiment-data').textContent = embeddedJSON(data); pause(); state = {...state, time: 0}; refreshExperiment(); el('scenario-dialog').close(); announce('Experiment recomputed. All views and exports now use this run.');
    } catch (error) { editorError(error); }
    finally { el('run-scenario').disabled = !session.live; text('run-scenario', 'Recompute experiment'); }
  });
  let capabilities = {link_replay: false, network_replay: false}, comparing = false;
  function comparisonError(error) { text('comparison-error', error?.message || ''); el('comparison-error').hidden = !error; }
  function comparisonControls(reset = false) {
    if (reset) { el('study-start').value = 0; el('study-duration').value = comparisonWindow(data.scenario).duration; }
    const window = comparisonWindow(data.scenario, Number(el('study-start').value), Number(el('study-duration').value));
    el('study-start').max = window.maximumOffset; el('study-duration').min = window.minimum; el('study-duration').max = window.maximum;
    text('comparison-sampling', window.supported ? 'Sampling interval: ' + formatNumber(window.minimum, 6) + ' s. Duration must include at least one declared interval.' : 'Comparison unavailable: reduce the scenario sampling interval to fit a comparison window of at most 3,600 s, then recompute.');
    el('packet-mode').querySelector('[value=link]').disabled = !capabilities.link_replay;
    el('packet-mode').querySelector('[value=network]').disabled = !capabilities.network_replay || !data.scenario.network;
    if (el('packet-mode').selectedOptions[0].disabled) el('packet-mode').value = 'none';
    const mode = el('packet-mode').value;
    el('packet-fields').hidden = mode === 'none';
    for (const id of ['study-load', 'study-size', 'study-queue', 'study-flows', 'study-acquisition']) el(id).disabled = mode === 'none' || !session.live || comparing;
    if (mode !== 'network') { el('study-flows').value = 1; el('study-acquisition').value = 0; el('study-flows').disabled = true; el('study-acquisition').disabled = true; }
    text('comparison-scenario', data.scenario.name);
    text('comparison-selection', mode === 'link' ? 'Replay: ' + data.stations[state.station].name + ' · NORAD ' + data.satellites[state.satellite].norad_id : mode === 'network' ? 'Replay endpoints: ' + data.scenario.network.source_station + ' → ' + data.scenario.network.target_station : 'Cases follow models declared in this scenario; no weather is inferred.');
    el('comparison-fields').disabled = !session.live || comparing || !window.supported;
    el('run-comparison').disabled = !session.live || comparing || !window.supported || !window.valid;
    if (!session.live) text('comparison-backend', 'Offline report: start the local application to compute another comparison. Stored metrics remain available; server report URLs require the live session.');
  }
  function summaryTable(headers, rows) {
    const wrap = document.createElement('div'), table = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
    wrap.className = 'table-scroll'; const title = document.createElement('tr');
    headers.forEach(value => { const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = value; title.append(cell); }); head.append(title);
    rows.forEach(values => { const row = document.createElement('tr'); values.forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }); body.append(row); });
    table.append(head, body); wrap.append(table); return wrap;
  }
  function renderComparison(summary) {
    if (!summary?.cases?.length) return;
    const cases = summary.cases.map(item => {
      const article = document.createElement('article'), heading = document.createElement('h4'), note = document.createElement('p');
      heading.textContent = item.label || item.id; note.className = 'muted'; note.textContent = formatNumber(item.duration_s) + ' s · ' + formatNumber(item.sample_count, 0) + ' samples'; article.append(heading, note);
      if (item.station_metrics?.length) article.append(summaryTable(['Station', 'Visible samples', 'Usable samples', 'Best-link bits', 'Fixed bits', 'Mean rate', 'RF outage / s', 'Handovers'], item.station_metrics.map(station => [station.name, percent(station.visible_sample_fraction), percent(station.usable_sample_fraction), formatSI(station.best_link_integrated_bits, 'bit'), formatSI(station.fixed_baseline_integrated_bits, 'bit'), formatSI(station.mean_best_rate_bps, 'bit/s'), formatNumber(station.rf_outage_duration_s), formatNumber(station.handover_count, 0)])));
      if (item.route_metrics?.length) article.append(summaryTable(['Route model', 'Connected samples', 'Integrated bits', 'Connected / s', 'Mean rate', 'Changes'], item.route_metrics.map(route => [route.routing_model, percent(route.connected_sample_fraction), formatSI(route.integrated_bottleneck_bits, 'bit'), formatNumber(route.connected_duration_s), formatSI(route.mean_bottleneck_bps, 'bit/s'), formatNumber(route.route_changes, 0)])));
      for (const packet of item.packets || []) { const details = document.createElement('details'), title = document.createElement('summary'), metrics = document.createElement('pre'); title.textContent = 'Packet metrics · ' + packet.mode + (packet.routing_model ? ' · ' + packet.routing_model : ''); metrics.textContent = JSON.stringify(packet.summary, null, 2); details.append(title, metrics); article.append(details); }
      return article;
    });
    const limitations = (summary.limitations || []).map(value => { const li = document.createElement('li'); li.textContent = value; return li; });
    text('comparison-result-name', summary.name); text('comparison-result-source', 'Computed from: ' + (summary.scenario?.name || data.scenario.name));
    el('comparison-case-list').replaceChildren(...cases); el('comparison-limitations').replaceChildren(...limitations);
    el('comparison-results').hidden = false; el('comparison-links').hidden = !session.live;
    if (!session.live) text('comparison-retention', 'These stored comparison metrics travel with this offline report. Preserve the full case reports and packet CSVs by downloading the portable ZIP from the live session.');
  }
  el('comparison-open').addEventListener('click', () => { pause(); comparisonControls(true); comparisonError(null); el('comparison-dialog').showModal(); });
  for (const id of ['packet-mode', 'study-start', 'study-duration']) el(id).addEventListener('change', () => comparisonControls());
  el('comparison-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!session.live || comparing) return;
    comparisonError(null);
    try {
      if (!comparisonWindow(data.scenario, Number(el('study-start').value), Number(el('study-duration').value)).valid) throw new Error('Choose a comparison window that fits the source and includes at least one declared sampling interval. Reduce the scenario sampling interval and recompute if it exceeds 3,600 s.');
      const settings = {packet_mode: el('packet-mode').value};
      for (const [id, key] of [['study-start', 'start_offset_s'], ['study-duration', 'duration_s'], ['study-seed', 'seed'], ['study-load', 'offered_load_bps'], ['study-size', 'packet_size_bytes'], ['study-queue', 'queue_packets'], ['study-flows', 'flows_per_direction'], ['study-acquisition', 'acquisition_delay_s']]) {
        if (el(id).disabled) continue;
        if (!el(id).reportValidity()) throw new Error('Check the comparison settings.');
        settings[key] = Number(el(id).value);
      }
      if (settings.packet_mode === 'link') { settings.station_name = data.stations[state.station].name; settings.norad_id = data.satellites[state.satellite].norad_id; }
      comparing = true; comparisonControls(); text('run-comparison', 'Computing…'); text('comparison-status', 'Computing all declared cases…');
      el('comparison-form').setAttribute('aria-busy', 'true');
      const response = await fetch('/api/study', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-OpenLEO-Token': session.token}, body: JSON.stringify({scenario: data.scenario, settings})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || 'The local comparison failed.');
      const links = studyLinks(result);
      if (result.summary?.kind !== 'openleo.experiment' || !Array.isArray(result.summary.cases) || !result.summary.cases.length) throw new Error('The local process returned an incomplete comparison.');
      renderComparison(result.summary); el('comparison-report').href = links.report_url; el('comparison-archive').href = links.archive_url;
      el('comparison-data').textContent = embeddedJSON(result.summary); text('comparison-status', 'Comparison complete.');
    } catch (error) { comparisonError(error); text('comparison-status', 'Comparison failed; previous results are retained.'); }
    finally { comparing = false; comparisonControls(); el('comparison-form').setAttribute('aria-busy', 'false'); text('run-comparison', 'Run comparison'); }
  });
  if (session.live) fetch('/api/capabilities').then(async response => {
    if (!response.ok) throw new Error('Unable to check packet backend availability.');
    const value = await response.json(); capabilities = {link_replay: value.link_replay === true, network_replay: value.network_replay === true}; comparisonControls();
    text('comparison-backend', capabilities.link_replay || capabilities.network_replay ? 'Packet replay uses the trusted backend configured at local server startup.' : 'Model-only comparisons are available. For packet replay, configure trusted backends at local server startup.');
  }).catch(error => text('comparison-backend', error.message + ' Model-only comparison remains available.'));
  comparisonControls(true); renderComparison(read('comparison-data'));
  new ResizeObserver(() => { drawGlobe(); drawCharts(); }).observe(el('main'));
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', event => { if (event.matches) pause(); });
  state = {...state, satellite: initialSatellite(data, state.station)}; refreshExperiment(); focusStation(true); pause();
})();
