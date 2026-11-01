# Lucid Triton TRI089S-M — camera white paper (generated 2026-11-01 13:11 UTC from the camera catalog; runs: 244500230/20261027T083623Z)

What this camera is, how it works, how it behaves and how well it performs, measured on the
camera_lab bench. Machine-readable twin: `camera.yaml`.

**Conditions:** all 12-bit (`raw`) numbers are at **Gain 0 dB and BlackLevel 1**, which the
`raw` mode pins. Gain above 0 dB trades full well for nothing useful in this mode (see § 7).

Tags: **[measured date]** our bench · **[fingerprint date]** the bench fingerprint ·
**[node map]** read from the live camera · **[manual]** Lucid documentation · **[to measure]**
not measured yet. Numbers of one unit (hot pixels, exact black level) are in that serial's
fingerprint: `WS/config/camera_fingerprints/<serial>/`.

## 1. Summary
An 8.9 MP monochrome camera (4096 × 2160, 3.45 µm pixels, ≈ 1" optical format) with a
**global shutter**: every pixel starts and stops its exposure at the same moment, so moving
parts are not skewed and rippling light changes the whole frame's brightness instead of
drawing bands. 12-bit ADC, no HDR, no colour. Out of the box it runs **auto exposure** and
8-bit output; linear 12-bit data needs the `raw` mode. Measured on unit 244500230:
**read noise 2.4 e⁻**, **full well ≈ 10 900 e⁻**, **73 dB (12.2 stops)** dynamic range,
linearity error 0.26 %, PRNU 1.7 %. [fingerprint 2026-10-27]

### On site (no test bench): the sequence and the rules
1. **Start from a known state:** `camera-lab two-d reset <camera>` (MCP `reset_camera`):
   factory Default, then the design mode with all its settings, every one read back.
2. **Prove the state on the image:** `two-d verify` (MCP `verify_camera_state`): design-mode
   settings in place, gain 0 dB, linear (× 2.00 per exposure doubling), no dark pixel at 0.
3. **Judge the light:** `two-d assess-light` (MCP `assess_site_light`): flicker, enough light,
   SNR against § 6, clipping, scene structure, distribution, light stability — each with an
   action. It resets the camera itself, measures, then **puts the camera back as it was**;
   `--apply` leaves it in the design mode at the recommended exposure.
4. **Set the exposure:** `two-d tune <camera> --mode raw` (exposure is the only knob), or the
   exposure assess-light recommends.
5. **Focus** is a human job at the lens: it needs texture in view (a chart, print, or any
   sharp edge); `two-d setup` and the Focus tool show the live MTF50.

**Which mode for what:** `raw` (Mono12p, linear, § 3) for everything a machine measures or
detects. `linear` (exposure, then gain) only when a job cannot get enough light in its time
budget and accepts the noise. 8-bit output only for a human looking at the picture.

**One brightness rule (`raw`):** expose to the right — the highlights (99th percentile) at
about **80 % of full scale**, never clipped (≤ 0.2 % of pixels). At 80 % the SNR is ≈ 96, close
to the shot-noise limit √(full well) ≈ 104 (§ 6). The tuner, the capture scores
(`level_metric: p99`), the site tool and this paper all use this rule.

**Exposure budget:** one frame at the frame rate the job needs. In `raw` (Mono12p) the camera
runs at most 8.39 fps full frame (GigE bandwidth) → one frame ≈ 119 ms; Mono8: 12.6 fps ≈
79 ms. Longer exposures lower the frame rate (camera_lab raises it back as the exposure
shortens). Too dark within the budget → more light (or a wider iris) first; gain only up to
24 dB, and only when the light cannot be raised (§ 7).

**Boot state:** this unit boots from the factory **Default** user set, which runs **auto
exposure** (`ExposureAuto = Continuous`) in Mono8: the brightness drifts after every
power-up. Every tool, and the production software, must apply the design mode itself
(`reset`); never rely on what the camera booted with. A site agent does not save user sets.

**Files:** every command that changes settings first backs the camera up
(`config/camera_backups/<serial>/`); captures, runs and reports go under the data folder.

## 2. Architecture and signal path
| stage | what happens | notes |
|---|---|---|
| sensor | mono, **global shutter** (`SensorShutterMode = Global`, the only choice), 4096 × 2160, 3.45 µm | [node map], pixel pitch [manual] |
| ADC | 12-bit (`ADCBitDepth = Bits12`, the only choice) | [node map] |
| gain | `Gain` 0–48 dB: **analog 0–24 dB, digital 24–48 dB** | [manual: Triton gain table, TRI089S row] |
| black level | `BlackLevel` 0–12.5 % of 4095 added to every output | [node map], [fingerprint 2026-10-27] |
| defect correction | `DefectCorrectionEnable`, on by default: the camera patches its factory list of defective pixels (10 on this unit) | [measured 2026-10-28] |
| gamma | `Gamma` 0.2–2.0 with `GammaEnable`; on by default **at 1.0** (linear) | [node map] |
| LUT | `LUTEnable`, off by default | [node map] |
| sharpening | `SharpeningEnable`, off by default; camera_lab never turns it on | [node map] |
| output | Mono8, Mono10 / 10p / 10Packed, Mono12 / 12p / 12Packed, Mono16; ROI, binning 1–8, horizontal decimation 1–2, ReverseX / Y | [node map] |

## 3. Output formats and their meaning
| output | linear? | use |
|---|---|---|
| **Mono12p**, gamma 1.0, LUT off (`raw`) | **yes** (× 2.00 signal per exposure doubling) | measurement, detection: the design mode |
| Mono16 | yes; the 12-bit value in a 16-bit word, at 6.3 fps (more bytes, no more information) | when a tool cannot read packed formats |
| Mono8 | yes at gamma 1.0, but 16× coarser steps: read noise (0.9 DN at 12 bit) is below one 8-bit step, so the noise floor and the darkest tones vanish | display; fast live view (12.6 fps) |
| any format with gamma ≠ 1.0 or the LUT on | no | display only |

12-bit packed formats are unpacked to uint16 by the driver. Full scale = 4095 DN.

`BlackLevel` (0–12.5 %, 0 by default) adds its % of 4095 to the black: at 1 the black sits at
**41.3 DN**. `raw` sets 1 so dark noise is never clipped at 0 (as on the TRI054S-C, where
BlackLevel 0 left 64 % of dark pixels at 0; whether this sensor clips at 0 is [to measure]).
[fingerprint 2026-10-27]

## 4. Controls and their behaviour
| control | behaviour | source |
|---|---|---|
| exposure | `ExposureTime` from **47.2 µs**; the maximum follows the frame period (79 ms at Mono8 free-run); camera_lab lowers the frame rate for longer exposures and restores free-run afterwards | [node map], [measured 2026-10-21] |
| exposure auto | `ExposureAuto` **Continuous in factory Default**; camera_lab turns every auto function off on open | [node map], [measured 2026-10-26] |
| gain | 0–48 dB; analog to 24 dB, digital beyond (§ 7); `raw` pins 0 dB | [manual], [fingerprint 2026-10-27] |
| black level | shifts the black by its % of 4095; it also moves with gain (41.4 DN at 0 dB, 37.9 at 33 dB, 66 at 48 dB) | [fingerprint 2026-10-27] |
| gamma | `Gamma` 0.2–2.0, applied while `GammaEnable` is on; keep 1.0 | [node map] |
| white balance | `BalanceRatio*` / `BalanceWhiteAuto` are listed but neither readable nor writable (mono): the driver treats them as absent | [measured 2026-10-21] |
| `PixelFormat`, `Width`, `Height` | locked while streaming: the driver writes them only when they change, with the stream stopped | [measured 2026-10-21] |
| `AcquisitionFrameRate` | only writable with `AcquisitionFrameRateEnable`; free-run by default | [node map] |
| trigger | `TriggerMode` off by default; sources Software, Line0, Line2, Line3, Action0; camera_lab sets it off on every open | [node map] |
| user sets | Default + UserSet1/2; any can be the boot set (`UserSetDefault`) | [node map] |
| PTP | `PtpEnable`, off by default (needed for exact exposure timestamps on site) | [node map] |

### Setting names (camera_lab key ↔ GenICam node)
| camera_lab | GenICam node | notes |
|---|---|---|
| `exposure_us` | `ExposureTime` | µs |
| `gain_db` | `Gain` | dB; analog to 24 dB, digital beyond |
| `gamma` | `Gamma` (+ `GammaEnable`) | keep 1.0 |
| `pixel_format` | `PixelFormat` | stream stop required |
| `frame_rate_hz` | `AcquisitionFrameRate` | needs `AcquisitionFrameRateEnable` |
| `roi` | `Width`, `Height`, `OffsetX`, `OffsetY` | width step 4, height step 2; stream stop required |
| `lut.enabled` | `LUTEnable` | stream stop required |
| `black.level` | `BlackLevel` | % of 4095 added to the black |
| profile mode `raw` | — | Mono12p + `gain_db` 0 + `gamma` 1.0 + `lut.enabled` off + `black.level` 1 |

### Start-up state: never trust the boot state
**Every tool starts the same way:** load the factory **Default** user set (live only, nothing
written to flash), apply the **complete** design-mode settings, read every one back, then
verify on the image. camera_lab: `camera-lab two-d reset <camera>` / MCP `reset_camera` / GUI
"Load camera defaults..." (the site light assessment and the fingerprint do it themselves).

**Factory Default** (`UserSetSelector = Default` → `UserSetLoad`) [measured 2026-10-26]:

| setting | factory Default |
|---|---|
| pixel format | Mono8 |
| exposure | **auto (Continuous)** |
| gain | 0 dB (auto off) |
| BlackLevel | 0 |
| gamma | on, 1.0 |
| LUT | off |
| sharpening / defect correction | off / on |
| frame rate | free run (12.6 fps at Mono8) |
| ROI / binning / decimation / trigger | full frame 4096 × 2160 / 1 / 1 / off |

**Design mode `raw` — the complete settings** (applied on top of Default): Mono12p, gain
0 dB, gamma 1.0, LUT off, BlackLevel 1, exposure auto off; everything else as Default.

**Verify** (§ 10): settings read back equal; on the image: doubling the exposure gives × 2.00;
no dark pixel at exactly 0; black ≈ 41 DN with the cap on.

**What a user set stores (survives a restart)** [fingerprint 2026-10-27, software reboot and
power cut identical, camera back in ≈ 31 s]: pixel format, ROI (width, height, offsets),
exposure, gain, gamma, LUT, black level — all 10 tested settings kept. Trigger mode and frame
rate were not tested (camera_lab sets both on every open). So a user set **can** hold the
whole `raw` mode: the fingerprint saved `raw` to UserSet1, restarted, and the camera came back
in the design mode.

**Boot set:** in the lab the camera boots from factory **Default**. On site, once the setup
is confirmed, it may boot from **User Set 1** (the site configuration), so a power blip brings
it back configured — a safety net only: production software still applies and verifies the
mode after every boot.

## 5. Timing
| | value | source |
|---|---|---|
| frame rate, full frame | **Mono8 12.59 fps · Mono12p 8.39 · Mono16 6.29** — the GigE link (125 MB/s, `DeviceLinkThroughputLimit`); use an ROI, binning or decimation for more speed | [fingerprint 2026-10-27] |
| one `raw` frame | 17.7 MB (Mono12p), a frame every 117 ms; 19 ms on the bench PC (x86_64, 24 CPUs) to a usable image | [fingerprint 2026-10-27] |
| shutter | **global**: all rows exposed together; no row time, no skew | [node map] |
| exposure vs frame rate | exposures longer than one frame period lower the frame rate (the driver does it, then restores free-run) | [measured 2026-10-21] |
| new settings visible after | 2 frame periods: Arena delivered frames up to 54 ms (of a 79 ms period) after a write still carrying the old settings; camera_lab skips them by the device clock: 0 stale frames | [measured 2026-10-22], [fingerprint 2026-10-27] |
| trigger-to-image delay | measured **on the machine** with the production trigger; exact exposure start needs PTP time sync. Bench estimate: exposure + readout + transfer (≈ 140 ms for a 17.7 MB frame over GigE) | [to measure on site] |

## 6. Measured performance (Mono12p, Gain 0 dB, BlackLevel 1)
Full scale = 2¹² − 1 = 4095 DN. One fingerprint run (3 repeats inside it agree within
0.1–0.2 %), unit 244500230, camera at 48–51 °C.

| property | value | fraction of full scale | source |
|---|---|---|---|
| black level | **41.3 DN** at BlackLevel 1 (1–100 ms); 46.7 DN at 2 s (dark current) | 1.0 % | [fingerprint 2026-10-27] |
| read noise (dark, temporal) | **0.88 DN = 2.4 e⁻** at 1–20 ms; 0.94 DN at 100 ms; 1.65 DN at 2 s | 0.02 % | [fingerprint 2026-10-27] |
| row noise (temporal, dark) | 0.14 DN rms (≈ 1/6 of the pixel noise) | | [fingerprint 2026-10-27] |
| DSNU | 0.38 DN at 1–10 ms, 0.66 DN at 100 ms, 7.1 DN at 2 s | | [fingerprint 2026-10-27] |
| system gain | **0.371 DN/e⁻** (≈ 2.7 e⁻ per DN) | | [fingerprint 2026-10-27] |
| full well | **≈ 10 900 e⁻** (≈ 4030 DN above black: the sensor fills just below the 12-bit top) | | [fingerprint 2026-10-27] |
| **dynamic range** | **73.2 dB (12.2 stops)** | | [fingerprint 2026-10-27] |
| dark current | 2.7 DN/s ≈ 7 e⁻/s at ≈ 50 °C: negligible below 100 ms | | [fingerprint 2026-10-27] |
| linearity error | **0.26 %** of range | | [fingerprint 2026-10-27] |
| shot-noise SNR limit | √(full well) ≈ **104**; measured 96 at 79 % of full scale | | [fingerprint 2026-10-27] |
| pixel response non-uniformity | **1.66 %** | | [fingerprint 2026-10-27] |
| vignetting | corners 0.32 of centre with the Computar 8 mm at f/2.8 (0.55 at f/5.6); lens- and iris-dependent: see the lens paper | | [fingerprint 2026-10-27] |
| hot pixels | unit-specific: 1542 at 1 ms, 5119 at 100 ms, ≈ 18 000 at ≥ 0.5 s on 244500230 | | [fingerprint 2026-10-27] |
| frame averaging | 16 frames: noise ÷ 4.01 (√16 = 4.00): no fixed pattern limits averaging | | [fingerprint 2026-10-27] |
| sharpness (Computar 8 mm, chart at ≈ 65 cm) | MTF50 0.223 / 0.225 / **0.233** / 0.218 / 0.183 / 0.128 cycles/px at f/2.8 / 4 / 5.6 / 8 / 11 / 16; best f/5.6 | | [measured 2026-10-27/28] |
| sharpness at f/5.6, resolution sheet near the image centre (≈ 68 cm) | MTF50 **0.252** cycles/px (10 readings 0.250–0.254); horizontal 0.263, vertical 0.253; 0.229 in the lower-right quarter (≈ 65 cm). Steady across exposure: 0.2515–0.2544 with highlights from 0.10 to 0.75 of full scale | | [measured 2026-10-31] |

**SNR vs signal level:** follows the shot-noise model up to ≈ 80 % of full scale (13.5 at
1.7 %, 51 at 24 %, 72 at 47 %, 96 at 79 %). The 122 and 178 measured at 90 % and 95 % are
above the √(full well) limit: pixels there are clipping, which hides their noise — not a real
gain. Expose highlights to ≈ 80 %, not higher.

## 7. Gain: analog to 24 dB, digital beyond
The TRI089S has **analog gain 0–24 dB and digital gain 24–48 dB** [manual]. Measured at a
constant output of ≈ 1000 DN [fingerprint 2026-10-27]:

- Each 3 dB costs ≈ 1/2 stop of headroom: full well 10 800 e⁻ at 0 dB, 2750 at 12 dB, 690 at
  24 dB, 39 at 48 dB.
- Read noise in electrons falls only from **2.4 e⁻ at 0 dB to 1.4 e⁻ at 24 dB**.
- In very low light (iris closed, fixed 0.87 ms exposure, 4 DN of signal): SNR **2.8 at
  0 dB, 3.1 at 24 dB, 3.1 at 48 dB**. Gain buys ≈ 10 % SNR up to 24 dB and nothing after.
- Averaging N frames improves SNR by √N exactly (16 frames: × 4.0), far more than any gain.

**Rule:** keep 0 dB. More light, a wider iris, a longer exposure or frame averaging come
first. If the time budget forces gain, stay ≤ 24 dB (analog); above that the image only gets
brighter, not better.

## 8. Limitations and known behaviours
- **Auto exposure in factory Default:** a camera opened by any other software after a power-up
  runs auto exposure; brightness differs between boots. camera_lab turns every auto function
  off on open; production software must do the same.
- **Digital gain above 24 dB:** brighter, not better (§ 7).
- **8-bit output hides the noise floor** (read noise 0.9 DN at 12 bit is 0.06 of an 8-bit
  step) and the shadows.
- **GigE bandwidth limits the frame rate:** 8.4 fps for full 12-bit frames; ROI, binning or
  decimation for more.
- **Stale frames after a write:** Arena can deliver frames up to 54 ms after a settings change
  that still carry the old settings; camera_lab skips them by the device clock. Software that
  does not must wait two frame periods.
- **Stream-locked controls:** `PixelFormat`, `Width`, `Height`, `LUTEnable` need the stream
  stopped.
- **Global shutter and flicker:** rippling light does not draw bands; it changes the whole
  frame's brightness from frame to frame. Exposures shorter than the ripple period are
  exposed to it; the shortest bench exposure is 0.5 ms.
- **Hot pixels grow fast with exposure** (× 3 from 1 to 100 ms, × 12 at ≥ 0.5 s): long
  exposures need the unit's dark frame subtracted.
- **Lens:** the Computar 8 mm resolves more horizontally than vertically when wide open
  (MTF50 0.26 vs 0.20 at f/2.8), converging when stopped down: likely astigmatism. Close up
  (≈ 23 cm) it is much softer (0.095 at f/4). [measured 2026-10-27/28]

## 9. Diagnostic signatures (what a misconfigured or failing camera looks like)
| signature | meaning |
|---|---|
| brightness drifts by itself between frames or boots | auto exposure is on (factory Default); run `two-d reset` |
| doubling exposure gives ≠ × 2.00 | gamma ≠ 1.0 or the LUT is on |
| the finest step between shadow values is > 1 DN | digital gain is on (> 24 dB) |
| many dark pixels read exactly 0 | BlackLevel 0: set `black.level` ≥ 1 |
| black not ≈ 41 DN with the cap on (10 ms) | BlackLevel is not 1, or gain is not 0 dB (the black moves with gain) |
| SNR in the highlights above ≈ 104 | those pixels are clipping |
| frame rate lower than in § 5 | exposure longer than one frame period, a larger pixel format, a slow link, or triggering |
| the image still shows the old settings just after a write | stale frames: wait two frame periods |
| whole-frame brightness flickers, no bands | rippling light with a short exposure (global shutter) |
| black level or read noise far from § 6 with the cap on | camera fault or heat (read `DeviceTemperature`) |
| hot-pixel count far above the unit's fingerprint | sensor ageing or damage |

## 10. Checking the camera's state
| test | method | expected |
|---|---|---|
| linear output | steady scene, exposure E and 2E (5–40 % of full scale) | signal × 1.95–2.05 |
| dark | lens cap on, 10 ms, Gain 0, BlackLevel 1 | black ≈ 41 DN, read noise ≈ 0.9 DN, no pixel at 0 |
| no digital gain | finest step between shadow values | 1 DN |
| no auto exposure | `ExposureAuto` reads Off; brightness steady over 10 frames | steady within the noise |
| no in-camera sharpening | `SharpeningEnable` reads off (`two-d reset` and `verify` check it; camera_lab never writes it) | off |
| start-up | `two-d reset` / `reset_camera` | all design-mode settings read back equal |
| hot pixels | lens cap on, 100 ms | close to the unit's fingerprint count |
| settling | change exposure, grab 3 frames | 0 stale frames |

## 11. Open questions
- Does BlackLevel 0 clip dark noise at 0 on this sensor, as on the TRI054S-C? [to measure]
- Temperature: the camera runs at 48–51 °C on the bench; dark current and black level at
  other temperatures are not measured.
- Read noise in electrons keeps falling above 24 dB (1.4 → 0.8 e⁻) although that gain is
  digital: likely the black shift with gain (§ 4) and quantization, not a real gain. The
  low-light SNR shows no benefit.
- Corner sharpness of the Computar 8 mm: the field chart will measure it (not printed yet).
- Trigger-to-image delay and PTP timing on the machine. [to measure on site]

## 12. Test conditions and sources
- Bench: DC LED floods at full (the bench light since 2026-10-23), opal acrylic diffuser on
  the lens, Computar 8 mm f/2.8–f/16 (1.1", C-mount), lens cap for darks. Unit 244500230,
  firmware 1.97.0.0 (device version RQQ51).
- Camera profile: `config/camera_profiles/244500230.yaml`
- Live node map: `docs/cameras/TRI089S-M_244500230_fw1.97.0.0_nodemap.tsv`
- Fingerprint: `WS/config/camera_fingerprints/244500230/20261027T083623Z/`
- Bench tests: `tests/two_d/test_tri089s_hw.py`
- Gain ranges: the Triton gain table in the Lucid TRI054S documentation (firmware 1.138.0.0),
  `Manuals/vision/lucid_triton/` (no TRI089S manual is stored).

## 13. Measured data (generated from the catalog)
Every number below is from the catalog (`camera-lab two-d catalog runs`).

| run | kind | lens | iris | °C | used |
|---|---|---|---|---|---|
| 244500230/20261027T083623Z | fingerprint | computar_f_1_2_8_1_1_c | f/2.8 | 48.0–51.0 | yes |

### Headline numbers per fingerprint

| run | black DN | read noise DN | row noise DN | dark current DN/s | DN/e- | full well e- | linearity % | PRNU % |
|---|---|---|---|---|---|---|---|---|
| 20261027T083623Z | 41.34 | 0.8827 | 0.1408 | 2.685 | 0.3705 | 1.088e+04 | 0.2593 | 1.659 |

### Darks per exposure (244500230/20261027T083623Z)

| exposure ms | black DN | read noise DN | hot pixels |
|---|---|---|---|
| 1 | 41.34 | 0.8827 | 1542 |
| 2 | 41.34 | 0.8824 | 1527 |
| 5 | 41.35 | 0.885 | 1577 |
| 10 | 41.36 | 0.8869 | 1672 |
| 20 | 41.39 | 0.8933 | 1632 |
| 50 | 41.47 | 0.9093 | 3229 |
| 100 | 41.61 | 0.9371 | 5119 |
| 200 | 41.88 | 0.9876 | 5200 |
| 500 | 42.68 | 1.126 | 1.75e+04 |
| 1000 | 44.03 | 1.327 | 1.884e+04 |
| 2000 | 46.71 | 1.655 | 1.851e+04 |

### Noise vs gain (244500230/20261027T083623Z)

| gain mode | gain dB | SNR | DN/e- | read noise e- | full well e- |
|---|---|---|---|---|---|
| master | 0 | 51.6 | 0.3765 | 2.36 | 1.077e+04 |
| master | 3 | 43.5 | 0.5286 | 2.13 | 7669 |
| master | 6 | 36.5 | 0.7431 | 1.95 | 5455 |
| master | 9 | 30.8 | 1.048 | 1.84 | 3868 |
| master | 12 | 25.9 | 1.473 | 1.74 | 2752 |
| master | 15 | 21.8 | 2.079 | 1.65 | 1950 |
| master | 18 | 18.2 | 2.929 | 1.57 | 1384 |
| master | 21 | 15.3 | 4.127 | 1.51 | 982 |
| master | 24 | 13 | 5.849 | 1.44 | 693 |
| master | 27 | 10.9 | 8.251 | 1.43 | 491 |
| master | 30 | 9.1 | 11.68 | 1.41 | 347 |
| master | 33 | 7.6 | 16.53 | 1.34 | 245 |
| master | 36 | 6.3 | 23.56 | 1.24 | 172 |
| master | 39 | 5.3 | 33.75 | 1.12 | 120 |
| master | 42 | 4.4 | 48.77 | 1 | 83 |
| master | 45 | 3.6 | 71.27 | 0.91 | 57 |
| master | 48 | 2.9 | 105 | 0.83 | 39 |

### Low light (244500230/20261027T083623Z)

| signal DN | SNR | model SNR |
|---|---|---|
| 4.28 | 2.82 | 2.78 |
| 8.494 | 4.31 | 4.29 |
| 17.08 | 6.41 | 6.41 |
| 43.36 | 10.55 | 10.56 |

### What a user set keeps (244500230/20261027T083623Z)

| setting | after a restart | how |
|---|---|---|
| pixel_format | kept | software reboot, UserSet2 |
| roi_width | kept | software reboot, UserSet2 |
| roi_height | kept | software reboot, UserSet2 |
| roi_x | kept | software reboot, UserSet2 |
| roi_y | kept | software reboot, UserSet2 |
| exposure_us | kept | software reboot, UserSet2 |
| gain_db | kept | software reboot, UserSet2 |
| gamma | kept | software reboot, UserSet2 |
| lut.enabled | kept | software reboot, UserSet2 |
| black.level | kept | software reboot, UserSet2 |

### Lenses tested on this camera

- `computar_f_1_2_8_1_1_c`: see `lenses/computar_f_1_2_8_1_1_c.md`

---
Generated from `WS/config/camera_catalog.sqlite`: edit the catalog (`camera-lab two-d catalog ...`), never this file.
