import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const ui = require('../src/openleo/_web/workbench.js');
const camera = {lon: 0, lat: 0};
const earth = 6378137;
const sampleLink = {station_index: 0, satellite_index: 0, elevation_deg: 30,
  range_m: 1200000, range_rate_mps: -100, doppler_hz: 733.8, rate_bps: 2000000,
  cn0_db_hz: 70, esn0_db: 10, modcod: 'QPSK 1/2'};
const experiment = {
  scenario: {name: 'current run'}, timestamps_utc: ['2026-09-10T12:00:00Z',
    '2026-09-10T12:01:00Z', '2026-09-10T12:02:00Z'],
  stations: [{name: 'Madrid'}], satellites: [{name: 'IRIDIUM 1', norad_id: 100}],
  links: [[sampleLink], [], [{...sampleLink, rate_bps: 0, modcod: null}]],
  network: {frames: [{minimum_delay: {nodes: [1, 0], delay_s: 0.004,
    bottleneck_bps: 2000000}, maximum_rate: null, fixed_capacity: null}, {}, {}]},
};

test('WGS84 coordinates put equator and pole on the ellipsoid', () => {
  assert.deepEqual(ui.ecef(0, 0), [earth, 0, 0]);
  const pole = ui.ecef(0, 90);
  assert.ok(Math.abs(pole[0]) < 1e-8);
  assert.ok(Math.abs(pole[2] - 6356752.314245) < 0.001);
});

test('orthographic rotation preserves depth and uses east to the right', () => {
  assert.deepEqual(ui.project([earth, 0, 0], camera), [0, 0, 1]);
  const east = ui.project([0, earth, 0], camera);
  assert.deepEqual(east, [1, 0, 0]);
  const rotated = ui.project([0, earth, 0], {lon: Math.PI / 2, lat: 0});
  assert.ok(Math.abs(rotated[0]) < 1e-12);
  assert.equal(rotated[2], 1);
});

test('ellipsoid occludes rear points but keeps satellites beyond the limb', () => {
  assert.equal(ui.visible([earth, 0, 0], camera), true);
  assert.equal(ui.visible([-earth, 0, 0], camera), false);
  assert.equal(ui.visible([-earth * 1.2, 0, 0], camera), false);
  assert.equal(ui.visible([-earth, earth * 1.2, 0], camera), true);
  assert.equal(ui.visible([0, 0, 6400000], camera), true);
  assert.equal(ui.visible([0, 0, -6356752.314245], {lon: 0, lat: Math.PI / 2}), false);
});

test('visual subdivisions stay bounded for huge finite coordinates and retain LEO detail', () => {
  assert.equal(ui.segmentCount([6378137, 0, 0], [6378137, 0, 0]), 1);
  assert.equal(ui.segmentCount([6378137, 0, 0], [7178137, 0, 0]), 7);
  assert.equal(ui.segmentCount([6378137, 0, 0], [10378137, 0, 0]), 32);
  assert.equal(ui.segmentCount([1e250, 0, 0], [-1e250, 0, 0]), 256);
  assert.equal(ui.segmentCount([Number.MAX_VALUE, 0, 0], [-Number.MAX_VALUE, 0, 0]), 256);
});

test('selected link lookup never carries earlier metrics across an outage', () => {
  assert.equal(ui.getLink(experiment, 0, 0, 0), sampleLink);
  assert.equal(ui.getLink(experiment, 1, 0, 0), null);
  assert.equal(ui.getLink(experiment, 0, 0, 1), null);
  assert.equal(ui.getLink(experiment, 9, 0, 0), null);
});

test('initial selection prefers a usable higher-rate link over elevation alone', () => {
  const data = {...experiment, links: [[{...sampleLink, rate_bps: 0, elevation_deg: 85},
    {...sampleLink, satellite_index: 1, elevation_deg: 20, rate_bps: 5000000}]]};
  assert.equal(ui.initialSatellite(data, 0), 1);
  assert.equal(ui.initialSatellite({...data, links: [[], data.links[0]]}, 0), 0);
  assert.equal(ui.initialSatellite({...data, links: [[]]}, 0), 0);
});

test('charts distinguish zero adaptive rate from absent geometry', () => {
  assert.deepEqual(ui.series(experiment, 0, 0, 'rate_bps'), [2000000, 0, 0]);
  assert.deepEqual(ui.series(experiment, 0, 0, 'elevation_deg'), [30, null, 30]);
  assert.deepEqual(ui.series(experiment, 0, 0, 'doppler_hz'), [733.8, null, 733.8]);
});

test('metric formatting preserves zero and negative Doppler with readable units', () => {
  assert.equal(ui.formatNumber(null), '—');
  assert.equal(ui.formatNumber(NaN), '—');
  assert.equal(ui.formatNumber(0), '0');
  assert.equal(ui.formatSI(-22000, 'Hz'), '-22 kHz');
  assert.equal(ui.formatSI(1500000, 'bit/s'), '1.5 Mbit/s');
  assert.equal(ui.formatSI(0, 'bit/s'), '0 bit/s');
});

test('embedded JSON cannot close its script element and retains exact values', () => {
  const value = {name: '</script><img src=x onerror=alert(1)>', line: '\u2028\u2029', n: 1.2345678901234567};
  const encoded = ui.embeddedJSON(value);
  assert.equal(encoded.includes('<'), false);
  assert.equal(encoded.includes('\u2028'), false);
  assert.deepEqual(JSON.parse(encoded), value);
});

test('standalone report exports the current run and strips the live session token', () => {
  const original = '<html><script type="application/json" id="experiment-data">{"old":true}</script>' +
    '<script type="application/json" id="session-data">{"live":true,"token":"secret-token"}</script>' +
    '<script>"unchanged application"</script></html>';
  const result = ui.standaloneHTML(original, {...experiment, scenario: {name: '</script>latest'}});
  assert.ok(result.startsWith('<!doctype html>'));
  assert.equal(result.includes('secret-token'), false);
  assert.equal(result.includes('"old":true'), false);
  assert.ok(result.includes('"live":false,"token":""'));
  assert.ok(result.includes('\\u003c/script>latest'));
  assert.ok(result.includes('<script>"unchanged application"</script>'));
  assert.throws(() => ui.standaloneHTML('<html></html>', experiment), /Missing/);
});

test('CSV contains the current exact numeric samples and escapes hostile names', () => {
  const data = {...experiment, stations: [{name: '=CMD("unsafe")'}]};
  const csv = ui.linksCSV(data);
  assert.ok(csv.includes('2026-09-10T12:00:00Z'));
  assert.ok(csv.includes('"\'=CMD(""unsafe"")"'));
  assert.ok(csv.includes('2000000'));
  assert.equal(csv.trim().split('\r\n').length, 3);
  assert.ok(ui.linksCSV({...data, links: [[], [], []]}).startsWith('timestamp_utc,'));
});

test('reference-atmosphere CSV adds exact propagation fields only for schema 2', () => {
  const propagation = {gaseous_dry_attenuation_db: 0.13, gaseous_water_attenuation_db: 0.07,
    gaseous_attenuation_db: 0.2, free_space_cn0_db_hz: 70.2, geometric_delay_s: 0.004,
    atmospheric_excess_delay_s: 0.000000032, apparent_elevation_deg: 30.04};
  const legacy = ui.linksCSV({...experiment, schema_version: '1'});
  assert.equal(legacy.includes('gaseous_attenuation_db'), false);
  const reference = ui.linksCSV({...experiment, schema_version: '2',
    links: [[{...sampleLink, ...propagation}], [], []]});
  assert.ok(reference.split('\r\n')[0].endsWith(Object.keys(propagation).join(',')));
  assert.ok(reference.split('\r\n')[1].endsWith(Object.values(propagation).join(',')));
});

test('route CSV includes disconnected snapshots rather than inventing paths', () => {
  const csv = ui.routesCSV(experiment);
  assert.ok(csv.includes('minimum_delay,true,0.004,2000000,Madrid → IRIDIUM 1'));
  assert.ok(csv.includes('maximum_rate,false,,,\r\n'));
  assert.equal(csv.trim().split('\r\n').length, 10);
});

const guidedScenario = {stations: [{name: 'A', latitude_deg: 10, longitude_deg: 20, height_m: 50},
  {name: 'B', latitude_deg: 0, longitude_deg: 0, height_m: 0}],
  network: {source_station: 'A', target_station: 'B'},
  propagation: {model: 'itu_reference', refinement: 1, station_heights_amsl_m: {A: 40, B: 0},
    hydrometeors: {model: 'declared_uniform_layers', stations: {
      A: {liquid_water_kg_m2: 1, rain_rate_mm_h: 2, rain_top_height_amsl_m: 5000, polarization_tilt_deg: 45},
      B: {liquid_water_kg_m2: 0, rain_rate_mm_h: 0, rain_top_height_amsl_m: 0, polarization_tilt_deg: 0}}}}};

test('guided rename synchronizes endpoints and both atmospheric maps immutably', () => {
  const before = JSON.stringify(guidedScenario);
  const renamed = ui.changeStation(guidedScenario, 0, 'rename', 'Renamed');
  assert.equal(renamed.stations[0].name, 'Renamed');
  assert.equal(renamed.network.source_station, 'Renamed');
  assert.deepEqual(renamed.propagation.station_heights_amsl_m, {Renamed: 40, B: 0});
  assert.equal(renamed.propagation.hydrometeors.stations.Renamed.rain_rate_mm_h, 2);
  assert.equal('A' in renamed.propagation.hydrometeors.stations, false);
  assert.equal(JSON.stringify(guidedScenario), before);
  assert.throws(() => ui.changeStation(guidedScenario, 0, 'rename', 'B'), /unique/);
  assert.throws(() => ui.changeStation(guidedScenario, 0, 'rename', ''), /name/);
});

test('guided add and removal keep every station map and distinct network endpoints valid', () => {
  const added = ui.changeStation(guidedScenario, 0, 'add');
  assert.equal(added.stations.length, 3);
  const name = added.stations[2].name;
  assert.equal(added.propagation.station_heights_amsl_m[name], 0);
  assert.deepEqual(added.propagation.hydrometeors.stations[name], {
    liquid_water_kg_m2: 0, rain_rate_mm_h: 0, rain_top_height_amsl_m: 0, polarization_tilt_deg: 0});
  const removed = ui.changeStation(added, 0, 'remove');
  assert.equal(removed.network.source_station, name);
  assert.equal(removed.network.target_station, 'B');
  assert.equal('A' in removed.propagation.station_heights_amsl_m, false);
  assert.throws(() => ui.changeStation(guidedScenario, 0, 'remove'), /two stations/);
});

test('declared atmosphere defaults cover all stations without inferring ellipsoid heights or weather', () => {
  const bare = {...guidedScenario}; delete bare.propagation;
  const gas = ui.changeAtmosphere(bare, 0, 'gas', true);
  assert.deepEqual(gas.propagation.station_heights_amsl_m, {A: 0, B: 0});
  const hydro = ui.changeAtmosphere(gas, 0, 'hydrometeors', true);
  assert.equal(hydro.propagation.hydrometeors.stations.A.rain_rate_mm_h, 0);
  const edited = ui.changeAtmosphere(hydro, 0, 'liquid_water_kg_m2', 2);
  assert.equal(edited.propagation.hydrometeors.stations.A.liquid_water_kg_m2, 2);
  assert.equal(edited.propagation.hydrometeors.stations.B.liquid_water_kg_m2, 0);
  assert.equal(hydro.propagation.hydrometeors.stations.A.liquid_water_kg_m2, 0);
  assert.throws(() => ui.changeAtmosphere(hydro, 0, 'rain_top_height_amsl_m', 20001), /rain top/i);
  assert.throws(() => ui.changeAtmosphere(hydro, 0, 'amsl', 100), /rain top/i);
  assert.equal(ui.changeAtmosphere(hydro, 0, 'polarization_tilt_deg', 180).propagation.hydrometeors.stations.A.polarization_tilt_deg, 180);
});

test('schema 3 CSV retains gas fields and adds exact cloud and rain metrics including zero', () => {
  const fields = {gaseous_attenuation_db: .2, cloud_attenuation_db: 0,
    rain_specific_attenuation_db_per_km: 2, rain_path_length_m: 3000,
    rain_attenuation_db: 6, hydrometeor_attenuation_db: 6.1,
    total_atmospheric_attenuation_db: 6.3};
  const rows = ui.linksCSV({...experiment, schema_version: '3', links: [[{...sampleLink, ...fields}], [], []]}).split('\r\n');
  const header = rows[0].split(','), values = rows[1].split(',');
  for (const [key, value] of Object.entries(fields)) assert.equal(values[header.indexOf(key)], String(value));
});

test('study response links must share a fixed local versioned experiment hash', () => {
  const hash = 'a'.repeat(64), report_url = `/api/study/${hash}/index.html`, archive_url = `/api/study/${hash}/archive.zip`;
  assert.deepEqual(ui.studyLinks({report_url, archive_url}), {report_url, archive_url});
  for (const bad of ['https://example.com/index.html', '/api/study/../index.html', `/api/study/${hash}/index.html?x=1`]) {
    assert.throws(() => ui.studyLinks({report_url: bad, archive_url}), /report/i);
  }
  assert.throws(() => ui.studyLinks({report_url, archive_url: `/api/study/${'b'.repeat(64)}/archive.zip`}), /archive/i);
});

test('comparison defaults and limits preserve the declared sampling interval', () => {
  const source = {time_window: {start_utc: '2026-09-10T12:00:00Z', stop_utc: '2026-09-10T12:02:00Z', step_s: 90}};
  assert.deepEqual(ui.comparisonWindow(source), {minimum: 90, maximum: 120, duration: 90,
    maximumOffset: 30, supported: true, valid: true});
  assert.deepEqual(ui.comparisonWindow(source, 30, 90), {minimum: 90, maximum: 90, duration: 90,
    maximumOffset: 30, supported: true, valid: true});
  assert.equal(ui.comparisonWindow(source, 31, 90).valid, false);
  assert.equal(ui.comparisonWindow(source, 0, 89).valid, false);
  const short = {time_window: {...source.time_window, stop_utc: '2026-09-10T12:00:30Z', step_s: 15}};
  assert.equal(ui.comparisonWindow(short).duration, 30);
  const long = {time_window: {...source.time_window, stop_utc: '2026-09-10T14:00:00Z', step_s: 3600}};
  assert.equal(ui.comparisonWindow(long).duration, 3600);
  assert.equal(ui.comparisonWindow(long).supported, true);
  const coarse = {time_window: {...long.time_window, step_s: 4000}};
  assert.equal(ui.comparisonWindow(coarse).supported, false);
  assert.equal(ui.comparisonWindow(coarse).valid, false);
  const micro = {time_window: {start_utc: '2026-09-10T12:00:00.123456Z', stop_utc: '2026-09-10T12:00:00.123458Z', step_s: .000001}};
  assert.equal(ui.comparisonWindow(micro).duration, .000002);
  assert.equal(ui.comparisonWindow(micro).supported, true);
});
