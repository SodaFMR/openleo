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

test('route CSV includes disconnected snapshots rather than inventing paths', () => {
  const csv = ui.routesCSV(experiment);
  assert.ok(csv.includes('minimum_delay,true,0.004,2000000,Madrid → IRIDIUM 1'));
  assert.ok(csv.includes('maximum_rate,false,,,\r\n'));
  assert.equal(csv.trim().split('\r\n').length, 10);
});
