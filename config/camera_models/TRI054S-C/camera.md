# Lucid Triton TRI054S-C — camera white paper (generated 2026-10-25 13:22 UTC from the camera catalog; runs: 233100505/20261024T084539Z)

What this camera is, how it works, how it behaves and how well it performs, measured on the
camera_lab bench. Machine-readable twin: `TRI054S-C.yaml`.

**Conditions:** all 24-bit (`raw_hdr`) numbers are at **Gain 0 dB and BlackLevel 1**, which
the `raw_hdr` mode pins. In this mode `Gain` is a digital multiplier after the HDR merge:
any other value scales every output level and clips highlights (see §8).

Tags: **[measured date]** our bench · **[manual]** Lucid documentation · **[to measure]**
the next fingerprint run fills it in. Numbers of one unit (hot pixels, exact black level)
are in that serial's fingerprint: `WS/config/camera_fingerprints/<serial>/`.

## 1. Summary
A 5.4 MP colour HDR camera built around Sony's IMX490 automotive sensor. Each pixel has two
sub-pixels read at two conversion gains; the sensor merges these four signals on the chip
into one linear 24-bit image — measured **137 dB (22.7 stops)** of dynamic range, above the
datasheet's > 120 dB — so very bright and very dark areas are captured in a single exposure. It has a rolling shutter. Out of the
box the camera applies a gamma-0.5 lookup table, which makes its output non-linear; its
linear raw data needs that table switched off. [manual], [measured 2026-10-22]

### On site (no test bench): the sequence and the rules
1. **Start from a known state:** `camera-lab two-d reset <camera>` (MCP `reset_camera`):
   factory Default, then the design mode with all its settings, every one read back.
2. **Prove the state on the image:** `two-d verify` (MCP `verify_camera_state`): design-mode
   settings in place, gain 0 dB, linear (× 2.00 per exposure doubling), no dark pixel at 0,
   finest shadow step 1 DN.
3. **Judge the light:** `two-d assess-light` (MCP `assess_site_light`): flicker, enough light,
   SNR against §6, clipping, scene structure, distribution, light stability — each with an
   action. It resets the camera itself, measures, then **puts the camera back as it was**;
   `--apply` leaves it in the design mode at the recommended exposure.
4. **Set the exposure:** `two-d tune <camera> --mode raw_hdr` (exposure is the only knob;
   gain and white balance are never touched in this mode), or the exposure assess-light
   recommends.
5. **Focus** is a human job at the lens: it needs texture in view (a chart, print, or any
   sharp edge); `two-d setup` shows the live MTF50. A featureless scene cannot be judged.

**Which mode for what:** `raw_hdr` (linear 24-bit, § 3) for everything a machine measures or
detects, and for production images that software processes. `hdr` / `linear` (factory LUT,
gamma 0.5, 8/12-bit) only for a human looking at the picture.

**One brightness rule (`raw_hdr`):** expose to the right — the highlights (99th percentile)
at about **80 % of full scale**, never clipped (≤ 0.2 % of pixels). The SNR is best near the
top (§ 6); the mean of an HDR scene then sits wherever the scene puts it. The tuner, the
capture scores (`level_metric: p99`), the site tool and this paper all use this rule.

**Exposure budget:** one frame at the frame rate the job needs. In `raw_hdr` (BayerRG24) the
camera runs at most 6.93 fps (GigE bandwidth) → one frame ≈ 144 ms; longer exposures lower the
frame rate (camera_lab raises it back as the exposure shortens). 8-bit modes: 20.8 fps ≈ 48 ms.
Too dark within the budget → more light (or a wider iris), never gain.

**Boot state:** this unit boots from the factory **Default** user set (since 2026-10-24), so
anything that opens it starts at gain 0 with no one's old settings — but in the display
state (8-bit, LUT on). Every tool, and the production software, must apply the design mode
itself (`reset`); never rely on what the camera booted with. A site agent does not save
user sets.

**Files:** every command that changes settings first backs the camera up
(`config/camera_backups/<serial>/`); captures, runs and reports go under the data folder.

## 2. Architecture and signal path
| stage | what happens | notes |
|---|---|---|
| sensor | Sony IMX490, Bayer RGGB, rolling shutter, 5.4 MP | [manual] |
| HDR capture | per pixel: sub-pixel 1 (large) and sub-pixel 2 (small), each read at high and low conversion gain → 4 channels | [manual] |
| channel gains | analog gain per channel; factory values 19.8 / 4.2 / 29.4 / 4.2 dB (ch0–ch3) | [measured 2026-10-21] |
| on-chip merge | the 4 channels combined into one linear 24-bit value per pixel | [manual], linear [measured 2026-10-22] |
| LUT | lookup table, **on by default, gamma 0.5** (`LUTEnable`, `LUTToneMapping = GammaPointFive`) | [measured 2026-10-22] |
| colour transformation | colour matrix, on by default | [manual] |
| output | Bayer 8/12/16/24-bit, RGB8 (debayered in the camera), or one HDR channel alone (`HDROutput = HDRChannel0…3`, 12-bit) | [manual], [measured 2026-10-22] |

## 3. Output formats and their meaning
| output | linear? | use |
|---|---|---|
| BayerRG24, LUT off, colour transformation off | **yes** (×2.00 signal per exposure doubling) | the raw HDR data: measurement, detection |
| any format with the factory LUT on | no (×1.41 per doubling: gamma 0.5) | display only |
| RGB8 | no (LUT + debayer in the camera) | display only |
| HDRChannel0…3, 12-bit | per channel; see section 7 | diagnosis of the sensor |

`BlackLevel` (0–12.5 %, 0 by default) adds its % of 4095 to the black of every output
(1 → +40 DN, 5 → +205 DN on the 24-bit output at 0 dB). At the default 0 the black sits at
≈ 0.7 DN and 64 % of dark pixels read exactly 0: dark noise is clipped. `raw_hdr` sets 1
(black ≈ 40 DN, nothing clipped, a negligible 40 of 16.7 M). [measured 2026-10-23]

## 4. Controls and their behaviour
| control | behaviour |
|---|---|
| exposure | sets all four HDR channel exposures together while HDR tuning is off; 86 µs minimum; beyond one frame period it lowers the frame rate | [measured 2026-10-21/22] |
| gain | HDR tuning **off** (`raw_hdr`): `Gain` is a **digital multiplier after the merge** — no SNR gain, it scales the output and clips the top (28.6 dB costs 4.75 stops); keep 0 dB. HDR tuning on: `ExposureTime` and `Gain` become read-only and the per-channel exposures/gains apply | [measured 2026-10-21/23] |
| black level | `BlackLevel` shifts the black of every output by its % of 4095; keep ≥ 1 so dark noise is not clipped at 0 | [measured 2026-10-23] |
| HDR image enhancement | on by default; **keep on**: switched off, temporal noise rises 10–32 % (most at the darkest levels) and the level drops ≈ 1 %; linearity unchanged (× 2.00). Whether it smooths spatially (fine detail) is not yet known | [measured 2026-10-24] |
| HDR digital clamping | on by default; no measurable effect on the linear output (noise ± 2 %, level ± 0.5 %) | [measured 2026-10-24] |
| HDR tuning (`HDRTuningEnable`) | off: one exposure for all channels; on: per-channel exposure and gain | [measured 2026-10-21] |
| `HDROutput`, `LUTEnable`, pixel format | **must be changed with the stream stopped**: written while streaming they are silently ignored | [measured 2026-10-22] |
| user sets | Default + UserSet1/2; a user set can be the boot set; it does **not** store HDR tuning or the HDR channel values | [measured 2026-10-21] |
| power-up state | boots from its boot user set with HDR tuning **off** and channel gains at the factory values | [measured 2026-10-21] |

### Setting names (camera_lab key ↔ GenICam node)
| camera_lab | GenICam node | notes |
|---|---|---|
| `exposure_us` | `ExposureTime` | µs; read-only while HDR tuning is on |
| `gain_db` | `Gain` | dB; digital after the merge in `raw_hdr` (keep 0); read-only while HDR tuning is on |
| `pixel_format` | `PixelFormat` | stream stop required |
| `frame_rate_hz` | `AcquisitionFrameRate` | needs `AcquisitionFrameRateEnable` |
| `white_balance` (red/green/blue) | `BalanceRatio` + `BalanceRatioSelector` | multiplies the raw data too: keep 1.0 for measurement |
| `hdr.enabled` | `HDRTuningEnable` | |
| `hdr.output` | `HDROutput` (`HDR`, `HDRChannel0…3`) | stream stop required |
| `hdr.enhancement` | `HDRImageEnhancementEnable` | |
| `hdr.clamping` | `HDRDigitalClampingEnable` | |
| `hdr.chN.exposure_us` | `HDRChannelExposureTime` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `hdr.chN.gain_db` | `HDRChannelAnalogGain` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `lut.enabled` | `LUTEnable` | stream stop required |
| `lut.tone_mapping` | `LUTToneMapping` (`Linear`, `GammaPointFive`, …) | |
| `color.transformation` | `ColorTransformationEnable` | |
| `black.level` | `BlackLevel` | % of 4095 added to the black of every output |
| profile mode `raw_hdr` | — | BayerRG24 + `gain_db` 0 + white balance 1/1/1 + `hdr.enabled` off + `hdr.output` HDR + `lut.enabled` off + `color.transformation` off + `black.level` 1 |

### Start-up state: never trust the boot state
A camera boots from whatever user set it was told to: someone's old settings (on our bench a
stray 28.6 dB digital gain skewed a day of measurements). **Every tool starts the same way:**
load the factory **Default** user set (live only, nothing written to flash), apply the
**complete** design-mode settings, read every one back, then verify on the image.
camera_lab: `camera-lab two-d reset <camera>` / MCP `reset_camera` (the site light
assessment and the fingerprint do it themselves).

**Factory Default** (`UserSetSelector = Default` → `UserSetLoad`) [measured 2026-10-23]:

| setting | factory Default |
|---|---|
| pixel format | BayerRG8 |
| exposure | 30 ms (auto off); HDR channel exposures follow it |
| gain | 0 dB (auto off) |
| BlackLevel | 0 |
| LUT | on, GammaPointFive (gamma 0.5) |
| colour transformation | on |
| HDR tuning / output | off / HDR; image enhancement and digital clamping on (keep both, see § 4) |
| HDR channel gains | 19.8 / 4.2 / 29.4 / 4.2 dB |
| white balance | **auto (Continuous)**, ratios ≈ 1.01 / 1.07 / 1.0 |
| frame rate | free run (19.7 fps at BayerRG8) |
| ROI / binning / trigger | full frame 2880 × 1860 / 1 / off |

**Design mode `raw_hdr` — the complete settings** (applied on top of Default):
BayerRG24, gain 0 dB, white balance 1.0 / 1.0 / 1.0 (auto off), LUT off, colour transformation
off, HDR tuning off, HDR output HDR, BlackLevel 1; everything else as Default.

**Verify** (§10): settings read back equal; on the image: doubling the exposure gives × 2.00;
the finest shadow step is 1 DN (no digital gain); no dark pixel at exactly 0.

**What a user set stores (survives a power cycle)** [measured 2026-10-24, software reboot and
power cycle identical]: pixel format, exposure, gain, black level, LUT, colour transformation,
white balance, HDR output / enhancement / clamping, frame rate, ROI, trigger (≈ 120 features
per `UserSetFeatureSelector`). **Not stored:** `HDRTuningEnable` and the HDR channel exposures
and gains (they come back at factory values). So a user set **can** hold the whole `raw_hdr`
mode (HDR tuning off is the factory value anyway).

**Boot set:** in the lab the camera boots from factory **Default** (no one's old settings).
On site, once the setup is confirmed, it may boot from **User Set 1** (the site configuration),
so a power blip brings it back configured — a safety net only: production software still
applies and verifies the mode after every boot. A software reboot (`DeviceReset`) behaves
like a power cycle (camera back in ≈ 20 s).

White balance multiplies the raw 24-bit data (black included) even with the colour
transformation off: keep it at 1.0 and balance colour on the PC. [measured 2026-10-23]

## 5. Timing
| | value | source |
|---|---|---|
| frame rate, full frame | 8-bit (Mono8 / BayerRG8) 20.8 fps · 12p 13.9 · 16-bit 10.4 · **24-bit (BayerRG24 / Mono24) and RGB8 6.93** · RGB12p 4.6 · RGB16 3.5 — GigE bandwidth (≈ 111 MB/s); use an ROI for more speed | [fingerprint 2026-10-24] |
| exposure vs frame rate | 61 ms → 16 fps, 122 ms → 8 fps, 244 ms → 4 fps | [measured 2026-10-21] |
| shutter | rolling: rows are exposed one after another over one readout (≈ one frame period) | [manual] |
| row (line) time | ≈ 23.5 µs per sensor row → ≈ 44 ms to read a full frame top to bottom | [measured 2026-10-23, from 120 Hz flicker bands] |
| triggered acquisition | max trigger rate ≈ half the free-run rate (a reset frame per trigger) | [manual] |
| trigger-to-image delay | measured **on the machine** with the production trigger (software or a robot-controller line); exact exposure start needs PTP time sync. Bench estimate: exposure + ≈ 44 ms readout + transfer (≈ 145 ms for a 16 MB BayerRG24 frame over GigE) | [to measure on site] |
| new settings visible after | 2 frame periods (camera_lab waits for them: 0 stale frames) | [measured 2026-10-22] |

## 6. Measured performance (linear 24-bit output, Gain 0 dB, BlackLevel 1)
Full scale = 2²⁴ − 1 = 16 777 215 DN. "Runs" = how many measurement runs the value rests on.

| property | value | fraction of full scale | runs / units | source |
|---|---|---|---|---|
| black level | ≈ 40 DN at BlackLevel 1 (204.6 DN at BlackLevel 5); ≈ 0.7 DN at BlackLevel 0, noise clipped | 0.00024 % | 3 / 1 (spread 0.09 %) | [fingerprint 2026-10-24] |
| read noise (dark, temporal) | **2.43 DN** at 1–10 ms (3.3 DN at 100 ms) | 0.000014 % | 3 / 1 (spread 0.95 %) | [fingerprint 2026-10-24] |
| row noise (temporal, dark) | 0.32 DN rms (≈ 1/8 of the pixel noise) | | 1 / 1 | [fingerprint 2026-10-24] |
| DSNU | 0.6 DN at 1–10 ms, 1.0 DN at 100 ms | | 1 / 1 | [measured 2026-10-23] |
| **dynamic range** (saturation ÷ read noise) | **137 dB (22.7 stops)** — above the datasheet's > 120 dB | | 3 / 1 | [fingerprint 2026-10-24] |
| saturation | the full 2²⁴ scale (sensor limit, not a digital clip) | 100 % | | [measured 2026-10-23] |
| dark current | 0 DN/s (lens cap under full floods: the cap is light-tight) | | 3 / 1 | [fingerprint 2026-10-24] |
| linearity error | **0.10 %** of range | | 3 / 1 | [fingerprint 2026-10-24] |
| peak SNR (flat, near full scale) | **312** | | 3 / 1 | [fingerprint 2026-10-24] |
| pixel response non-uniformity | **1.26 %** (R 1.41, Gr 1.26, Gb 1.29, B 1.59 %) | | 3 / 1 (spread ≤ 1.5 %) | [fingerprint 2026-10-24] |
| vignetting | corners 0.88 of centre (Kowa 8 mm f/1.4; lens- and iris-dependent) | | 3 / 1 | [fingerprint 2026-10-24] |
| colour under the bench floods (white LED) | **R/G 0.55, B/G 0.60** — a white-balance starting point (red × 1.82, blue × 1.67 on the PC); green imbalance Gr − Gb **+0.63 %** | | 3 / 1 (spread 0.1 %) | [fingerprint 2026-10-24] |
| SNR vs signal level | **not monotonic** — see the curve below | | | [measured 2026-10-23, confirmed 2026-10-24] |
| noise model σ² = a + b·signal | per HDR channel regime: see § 7 (gain DN/e⁻, read noise) | | | |
| hot pixels | unit-specific: 449 on 233100505 (8 σ, 100 ms, BlackLevel 5) | | | [fingerprint 2026-10-24] |
| flicker under the bench floods | adds 0.3 % to the noise at ≥ 2 ms (limit 2 %) | | | [fingerprint 2026-10-24] |

### SNR and effective resolution vs signal level (Gain 0 dB) [measured 2026-10-23, one run]
| level (% of full scale) | quantization step | channel in use | temporal noise σ | SNR |
|---|---|---|---|---|
| 0.03 % | 1 DN | ch0 | 162 DN | 27 |
| 0.05 % | 20 DN | ch1 | 227 DN | 39 |
| 0.2 % | 20 DN | ch1 | 453 DN | 80 |
| **0.4 %** | handover | ch1 → ch2 | 3 231 DN | **22 (dip)** |
| 0.8 % | 153 DN | ch2 | 4 210 DN | 33 |
| 1.7 % | 153 DN | ch2 | 5 801 DN | 48 |
| **3.3 %** | handover | ch2 → ch3 | 13 453 DN | **42 (dip)** |
| 6.7 % | 5 723 DN | ch3 | 18 148 DN | 62 |
| 13 % | 5 723 DN | ch3 | 21 820 DN | 104 |
| 27 % | 5 723 DN | ch3 | 28 117 DN | 161 |
| **53 %** | 5 723 DN | ch3 | 37 528 DN | **235** |
| **79 %** | 5 723 DN | ch3 | 46 261 DN | **287 (best)** |
| 100 % | saturation | | | |

- Four regimes, one per HDR channel: the step is one code of the channel in use (1, 20,
  153, 5 723 DN). The step ratios (20, 7.65, 37.4) are the channel sensitivity ratios and
  agree with the per-channel light measurements (22, 7.8, 36). The step is always far below
  the noise, so quantization never limits the data.
- **SNR dips at each handover** (≈ 0.4 % and ≈ 3.3 % of full scale) and is **best near the
  top** (≈ 235–290 at 50–80 %). Bright parts of a scene are the cleanest; parts sitting at a
  handover level are noisier than parts slightly darker.
- With digital gain the whole table shifts: at 28.6 dB every level is × 26.9, the handovers
  appear at ≈ 10 % and the top 4.75 stops clip (the 2026-10-23 morning numbers in the
  findings were taken that way).

## 7. The four HDR channels
| channel | sub-pixel / conversion gain | sensitivity vs ch3 | gain DN/e⁻ | full well e⁻ | read noise (dark) | repeat (3 runs) |
|---|---|---|---|---|---|---|
| ch0 | large, high (most sensitive) | ≈ 5700× (20× ch1) | **[to measure]**: needs ≈ 15× less light than f/16 (ND filter) | | 2.49 DN | |
| ch1 | large, low | ≈ 280× (7.8× ch2) | **0.246** | **15 808** | 0.74 DN = 3.0 e⁻ | 0.0 % |
| ch2 | small, high | ≈ 36× | **0.709** | **5 490** | 11.2 DN = 15.9 e⁻ (PTC 15.3 e⁻) | 0.3 % |
| ch3 | small, low (carries the highlights) | 1× | **0.0254** | **150 149** | 2.62 DN = 103 e⁻ | 1.6 % |

Measured on each channel's own 12-bit output (`HDROutput = HDRChannelN`), BlackLevel 5, under the
DC floods at ≥ 2 ms: ch3 at f/1.4, ch2 at f/8, ch1 at f/16 (fingerprint 2026-10-24). Linearity
error ≤ 0.19 % on every channel. The 2026-10-22 numbers (ch3 0.08 or 0.72, ch1 ≈ ch2) were
flicker-contaminated (dimmed panels, 0.1–3 ms exposures) and are withdrawn. Channel 3's
150 ke⁻ full well matches the combined output's best SNR (312 ≈ shot noise of ~100 ke⁻).

The sensitivities span ≈ 6000× from ch3 to ch0 (≈ 76 dB), which is how the merged image
reaches its dynamic range: ch0 resolves the shadows, ch3 the highlights. Ratios measured two
independent ways agree: ch1/ch2 = 7.8 (light per channel) vs 7.2 (quantization steps).
[measured 2026-10-22/23]. Measuring ch0's photon transfer needs very little light (≈ 15× less
than a Kowa 8 mm at f/16 under bright diffuse light): an ND filter or extra diffusion.

## 8. Limitations and known behaviours
- **Digital gain in `raw_hdr`:** `Gain` multiplies after the merge; anything above 0 dB
  only scales the output and clips the highlights. A user set left at 28.6 dB silently cost
  4.75 stops on our bench until `raw_hdr` pinned 0 dB. [measured 2026-10-23]
- **BlackLevel 0 clips dark noise** (64 % of dark pixels read 0); use ≥ 1.
  [measured 2026-10-23]
- **Rolling shutter:** a subject moving during the exposure and readout is skewed (≈ 44 ms
  from the top row to the bottom row).
- **Light ripple becomes row bands.** Any light whose intensity ripples faster than the
  exposure shows as horizontal bands that move between frames. The band period in **sensor
  rows** × 23.5 µs = the ripple period (one Bayer colour plane has half as many rows; the
  site tool reports sensor rows). Measured on the bench: dimmed LED panels 120 Hz (≈ 354 sensor
  rows per cycle; bands 2–3 % of the signal at 0.5–4 ms, gone at 8.3 ms = one mains half-cycle),
  "DC" LED floods ≈ 4 kHz (≈ 10 rows; 8 % at 0.1 ms, < 0.3 % from 2 ms). An exposure that is
  a whole number of ripple periods cancels it; longer exposures average it.
  [measured 2026-10-23]
- **Non-linear by default:** the factory LUT (gamma 0.5) is on; linear data needs
  `raw_hdr`-style settings (LUT off, colour transformation off, BayerRG24).
- **Stream-locked controls are ignored, not refused,** when written while streaming.
- **HDR settings do not survive a power cycle** through user sets.
- **Frame rate falls** when exposure exceeds one frame period, and halves with triggering.
- **8-bit output hides the noise floor** (noise below one 8-bit step at low gain).
- An SNR rise at 18 dB gain (vs 12 dB) was seen on the LUT output; likely a LUT effect.
  [measured 2026-10-21]

## 9. Diagnostic signatures (what a misconfigured or failing camera looks like)
| signature | meaning |
|---|---|
| doubling exposure gives × 1.41, not × 2.00 | factory LUT is on (gamma 0.5) |
| the finest step between pixel values in the shadows is > 1 DN (e.g. 26) | digital gain is on (step ≈ 10^(gain/20)); set `gain_db` 0 |
| many dark pixels read exactly 0 | BlackLevel 0: dark noise clipped; set `black.level` ≥ 1 |
| the 24-bit output clips at full scale while the scene is not that bright | digital gain is on |
| a written setting reads back changed but the image does not change | it was written while streaming (stream-locked control) |
| HDR tuning off and channel values back to factory after a power cycle | normal: user sets do not store HDR |
| `ExposureTime` / `Gain` read-only | HDR tuning is on |
| frame rate lower than expected | exposure longer than one frame period, or triggered acquisition |
| horizontal bands moving between frames | rippling light (rolling shutter): band period in rows × 23.5 µs = the light's ripple period (≈ 354 rows: 120 Hz mains; ≈ 10 rows: a switching LED driver) |
| slanted or wobbly vertical edges | subject moved during the exposure / readout |
| black level or read noise far from section 6 with the cap on | camera fault or heat (read `DeviceTemperature`) |
| hot-pixel count far above the unit's fingerprint | sensor ageing or damage |
| 8-bit output looks noise-free | normal: 8-bit hides the noise floor |

## 10. Checking the camera's state
| test | method | expected |
|---|---|---|
| linear output | steady scene, exposure E and 2E (5–40 % of full scale) | signal × 1.95–2.05 (× 1.41 = LUT on) |
| dark | lens cap on, 10 ms, Gain 0, BlackLevel 1 | black ≈ 40 DN, read noise ≈ 2.4 DN, no pixel at 0 |
| no digital gain | finest step between shadow values | 1 DN |
| start-up | `two-d reset` / `reset_camera` | all design-mode settings read back equal |
| hot pixels | lens cap on, 100 ms | close to the unit's fingerprint count |
| settling | change exposure, grab 3 frames | 0 stale frames |

## 11. Open questions
- **Channel 0 photon transfer:** needs ≈ 15× less light than a Kowa 8 mm at f/16 under the
  floods (an ND filter or extra diffusion).
- Temperature behaviour: not measured (operating range −20 to 55 °C ambient [manual]).
- Right after a reset the camera reports a frame-rate maximum of 6.56 fps (6.93 otherwise):
  tools take the budget from the measured 6.93 fps (§ 5), not from that reading.
- Does HDR image enhancement smooth spatially (would soften fine detail)? Needs a sharp target.

## 12. Test conditions and sources
- Bench: white board lit by two LED panels (10-step dimmers), opal acrylic diffuser on the
  lens, Kowa 8 mm f/1.4–f/16, lens cap for darks. Unit 233100505, firmware 1.138.0.0
  (device version K7A4W).
- Bench findings: `docs/experiments/20261021_findings.md`
- Camera profile: `config/camera_profiles/233100505.yaml`
- Fingerprints: `WS/config/camera_fingerprints/233100505/`
- Lucid TRI054S documentation (firmware 1.138.0.0): `Manuals/vision/lucid_triton/`

## 13. Measured data (generated from the catalog)
Every number below is from the catalog (`camera-lab two-d catalog runs`).

| run | kind | lens | iris | °C | used |
|---|---|---|---|---|---|
| 233100505/20261021T202300Z | fingerprint | - | - | - | history only |
| 233100505/20261021T203734Z | fingerprint | - | - | - | history only |
| 233100505/20261024T084539Z | fingerprint | kowa_8mm | f/1.4 | - | yes |

### Headline numbers per fingerprint

| run | black DN | read noise DN | row noise DN | dark current DN/s | DN/e- | full well e- | linearity % | PRNU % |
|---|---|---|---|---|---|---|---|---|
| 20261024T084539Z | 204.6 | 2.426 | 0.3221 | 0 | 0.02537 | 1.501e+05 | 0.1042 | 1.264 |

### Darks per exposure (233100505/20261024T084539Z)

| exposure ms | black DN | read noise DN | hot pixels |
|---|---|---|---|
| 1 | 204.6 | 2.426 | - |
| 10 | 205 | 2.542 | - |
| 100 | 203.8 | 3.761 | - |

### Lenses tested on this camera

- `kowa_8mm`: see `lenses/kowa_8mm.md`

---
Generated from `WS/config/camera_catalog.sqlite`: edit the catalog (`camera-lab two-d catalog ...`), never this file.
