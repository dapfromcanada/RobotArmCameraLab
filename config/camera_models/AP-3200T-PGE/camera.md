# JAI Apex AP-3200T-PGE — camera white paper (generated 2026-10-25 15:12 UTC from the camera catalog; runs: U330439/20261024T131257Z, U330439/20261024T161546Z, U330439/20261024T170000Z-live, U330439/20261025T101320Z, U330439/20261025T143049Z)

What this camera is, how it works, how it behaves and how well it performs, measured on the
camera_lab bench. Machine-readable twin: `AP-3200T-PGE.yaml`.

**Conditions:** all numbers are for the **12-bit raw output (`RGB12V1Packed`), master gain 0 dB,
white balance 1/1/1, BlackLevel 0 (default), one common exposure for the three sensors**,
which the `raw` mode pins.

Tags: **[measured date]** our bench · **[manual]** JAI AP-1600T/3200T-PGE user manual v2.2 ·
**[to measure]** the next fingerprint run or a site test fills it in. Numbers of one unit (hot
pixels, exact black level) are in that serial's fingerprint: `WS/config/camera_fingerprints/<serial>/`.

## 1. Summary
A 3.2 MP colour camera with **three global-shutter CMOS sensors behind a prism**: red, green and
blue each have their own full-resolution sensor (2064 × 1544, 3.45 µm), so there is no Bayer
pattern and no debayering, and every pixel has a true R, G and B value. The global shutter
exposes the whole frame at once, so moving parts are not skewed. Its strengths are colour
accuracy, resolution per colour and very clean, linear data: **0.11 % linearity error, 0.69 %
PRNU, 1.75 DN (≈ 2.0 e⁻) read noise**, the same in four fingerprints over two days (system gain
and full well within 0.4 %). Its limits are a small full well at the design gain mode
(**≈ 4 470 e⁻ → peak SNR ≈ 65, dynamic range 67 dB / 11.2 stops**; individual gain mode doubles
it to ≈ 9 460 e⁻ for little more read noise, § 4), the GigE link (**8 fps in 12-bit**), long
exposures (**above ≈ 100 ms the black level falls and the dark frame becomes uneven**, § 8) and
heat: it runs at **≈ 60–64 °C** inside and its black level drifts while it warms up. It is not an HDR
camera: scenes with very bright and very dark parts need the light or the exposure chosen for the
part that matters. [manual], [fingerprints 2026-10-24/25]

### On site (no test bench): the sequence and the rules
1. **Start from a known state:** `camera-lab two-d reset <camera>` (MCP `reset_camera`):
   factory Default, then the `raw` mode with all its settings, every one read back.
2. **Prove the state on the image:** `two-d verify` (MCP `verify_camera_state`): settings in
   place, gain 0 dB, linear (× 2.00 per exposure doubling).
3. **Judge the light:** `two-d assess-light` (MCP `assess_site_light`): enough light, SNR
   against § 6, clipping, structure, distribution, stability, each with an action. It puts the
   camera back as it was; `--apply` leaves it in `raw` at the recommended exposure. A global
   shutter shows no flicker bands; flickering light shows as frame-to-frame brightness changes.
4. **Set the exposure:** `two-d tune <camera> --mode raw` (exposure is the only knob; gain and
   white balance are never touched in this mode), or the exposure assess-light recommends.
5. **Focus** is a human job at the lens, with texture in view; `two-d setup` shows the live MTF50.

**Which mode for what:** `raw` (RGB12V1Packed, linear) for everything a machine measures or
detects. `prism` (8/10-bit with the camera's gamma 0.45 and white balance) only for a human
looking at the picture.

**One brightness rule (`raw`):** expose to the right — highlights (99th percentile) at about
**80 % of full scale**, never clipped (≤ 0.2 % of pixels). SNR rises steadily with signal (§ 6),
so brighter is always cleaner up to the clip.

**Warm-up:** the camera warms to ≈ 60 °C over the first quarter hour of streaming and its black
level drifts by ≈ 4 DN meanwhile. Warm (59.8–61.2 °C through a whole fingerprint, 2026-10-25) the
black holds within 1 % and there is no measurable dark current. Let it stream 15 minutes before
measuring, and compare dark readings only at similar `DeviceTemperature`.

**Exposure budget:** one frame at the frame rate the job needs. In `raw` the camera delivers at
most **8.0 fps** (GigE) → ≈ 125 ms per frame; 8-bit 12.0 fps ≈ 83 ms. Too dark within the budget
→ more light or a wider iris, never gain: even in low light gain improves SNR by only ≈ 12 %
(§ 4). Keep exposures at or below ≈ 100 ms (long exposures, § 8).

**Boot state:** this camera has **no boot-set selector**. It boots from factory Default until a
user set is saved; once one is saved it boots from **User Set 1**. In the lab nothing is saved.
On site the setup tool may save User Set 1 after a confirmed setup. Either way, every tool and
the production software apply the `raw` mode themselves (`reset`); never rely on the boot state.
A site agent does not save user sets.

**Files:** every command that changes settings first backs the camera up
(`config/camera_backups/<serial>/`); captures, runs and reports go under the data folder.

## 2. Architecture and signal path
| stage | what happens | notes |
|---|---|---|
| optics | C-mount lens → dichroic prism splits R, G, B onto three sensors | "Type 1/1.8 (F4 prism optical system)", 7.12 × 5.33 mm image [manual] |
| sensors | 3 × global-shutter CMOS, 2064 × 1544, 3.45 µm | the manual does not name the sensor [manual] |
| gain | analog, per sensor: master (`AnalogAll`, 0–18 dB) plus red/blue (−6.5 to +12 dB), or individual mode (0–36 dB each) | master 0 dB ≈ individual +6 dB [manual] |
| black level | `BlackLevel[DigitalAll]` −133 … +255 LSB at 12 bit, default 0 → black ≈ 100 DN | [manual], [fingerprint 2026-10-24] |
| shading, blemish correction | flat-field shading correction; white-blemish correction (≤ 200 pixels per sensor) | [manual] |
| gamma, colour matrix, white balance | gamma 0.45 by default, colour matrix, white balance | **bypassed in RGB12V1Packed** (gamma not even writable) [measured 2026-10-24] |
| output | RGB8, RGB10V1Packed, RGB10p32, RGB12V1Packed over GigE Vision (1000BASE-T, PoE) | [manual] |

## 3. Output formats and their meaning
| output | linear? | use |
|---|---|---|
| **RGB12V1Packed** | **yes** (gamma and white balance bypassed) | the raw data: measurement, detection |
| RGB8, RGB10 (gamma 0.45 default) | no | display only |

12-bit full scale is 4095 DN; the output clips at **4094 DN** (sensor/ADC, not a digital gain).
The black sits at ≈ 100 DN (2.4 % of full scale) at the default BlackLevel 0, so dark noise is
never clipped: no BlackLevel change is needed. [fingerprint 2026-10-24]

## 4. Controls and their behaviour
| control | behaviour |
|---|---|
| exposure | 14.7 µs to 8 s (performance verified to 1 s) plus a fixed sensor offset; one common exposure in `raw`, per sensor with `ExposureTimeMode = Individual` | [manual] |
| gain (master mode) | **analog**: × 7.8 DN/e⁻ at 18 dB (18 dB = × 7.9). Read noise in electrons falls only from 2.0 to 1.6 e⁻ while the full well falls from 4 530 to 582 e⁻: **gain costs headroom and buys almost nothing — keep 0 dB, add light** (table below) | [fingerprint 2026-10-24 16:15] |
| gain (low light, fixed exposure) | at a fixed exposure and light (8.5 DN at 0 dB) SNR goes from 2.6 at 0 dB to at best 2.9 at 9–18 dB (+12 %): gain does not rescue a dim scene — more light, a longer exposure (≤ 100 ms) or frame averaging (√N, verified to 16 frames) do | [fingerprint 2026-10-25 14:30] |
| gain mode | `IndividualGainMode` On ("individual") at 0 dB gives **half the signal** of master 0 dB (0.47–0.50×; manual: master 0 dB ≈ individual + 6 dB) and clips at twice the exposure: **full well ≈ 9 460 e⁻ (two runs 9 458 / 9 626), read noise 2.5 e⁻** (its own darks: 1.05 DN at 0.42 DN/e⁻), so **≈ 71.6 dB** against master's 66.5 dB — one stop more headroom for 0.4 e⁻ more read noise. `Gain` is not writable in that mode (per-sensor gains instead). **camera_lab's `raw` mode stays in master** (Derek 2026-10-25) | [fingerprint 2026-10-25 14:30] |
| white balance | per-sensor analog gain (`AnalogRed` / `AnalogBlue`); keep 1.0 in `raw` and balance colour on the PC | [manual] |
| black level | default 0 is right for `raw` (black 100 DN, nothing clipped) | [fingerprint 2026-10-24] |
| user sets | Default + UserSet1/2/3; `UserSetSave` needs the stream stopped; no boot-set selector; a user set keeps all 10 settings camera_lab can test (format, ROI size and offset, exposure, gain, gamma, white balance) across `DeviceReset` (≈ 20 s) **and across a real power cut — identically** | [fingerprint 2026-10-25 14:30] |
| frame rate | free run up to the interface limit (§ 5); `AcquisitionFrameRate` lower it | [manual] |

### Setting names (camera_lab key ↔ GenICam node)
| camera_lab | GenICam node | notes |
|---|---|---|
| `exposure_us` | `ExposureTime` | µs |
| `gain_db` | `Gain[AnalogAll]` | a multiplier on the camera; the driver converts to dB |
| `pixel_format` | `PixelFormat` | stream stop required |
| `frame_rate_hz` | `AcquisitionFrameRate` | |
| `white_balance` (red/green/blue) | `Gain[AnalogRed / AnalogGreen / AnalogBlue]` | keep 1.0 for measurement |
| `prism.exposure_mode` | `ExposureTimeMode` (`Common` / `Individual`) | |
| `prism.gain_mode` | `IndividualGainMode` (`Off` = master / `On`) | 0 dB differs by ≈ 6 dB between the two |
| `prism.exposure_red_us` … | `ExposureTime` per sensor | Individual mode |
| profile mode `raw` | — | RGB12V1Packed + gain 0 dB (master) + white balance 1/1/1 + common exposure |

### Start-up state: never trust the boot state
Every tool starts the same way: load factory **Default** (live only, nothing written to flash),
apply the **complete** `raw` settings, read every one back, then verify on the image.

**Factory Default** [measured 2026-10-24]: RGB8, exposure 82.4 ms, gain 0 dB, white balance 1/1/1,
free run, full frame. **Design mode `raw`** on top: RGB12V1Packed, gain 0 dB (master mode), white
balance 1/1/1, common exposure.

**What a user set stores** [fingerprint 2026-10-25 14:30]: every setting camera_lab could test — pixel format, ROI
(size and offset), exposure, gain, gamma, red and blue white balance — came back after a
software reboot (`DeviceReset`) and after the human unplugged the camera, identically: **a
software reboot equals a power cycle on the JAI**. Not testable through camera_lab: trigger,
frame rate, exposure mode and gain mode (camera_lab sets them whenever it opens the camera),
per-sensor exposures (they need the individual exposure mode), green white balance (fixed).
**The site setup works:** the `raw` mode saved to User Set 1 as on site came back after a
restart without a reset.

## 5. Timing
| | value | source |
|---|---|---|
| frame rate, full frame | **RGB8 12.0 fps · RGB10 9.0 · RGB12V1Packed 8.0** — GigE bandwidth; the sensors could do 24.2 fps; use an ROI (height) or binning for more speed | [fingerprint 2026-10-24], same as [manual] |
| one 12-bit frame | 14.3 MB on the wire (116 MB/s at 8 fps), 19.1 MB in memory (3 × 16-bit planes) | [fingerprint 2026-10-24] |
| image pipeline | a frame every **123 ms** (82 ms exposure); **20 ms on this PC** (x86_64, 24 CPUs) to a float image — no debayer needed | [fingerprint 2026-10-24] |
| shutter | global: every pixel exposed over the same interval; no row skew, no flicker bands | [manual] |
| exposure vs frame rate | exposure overlaps readout; beyond one frame period camera_lab lowers the frame rate. The camera keeps a readout margin of **1.1 ms in RGB12V1Packed** (0.74 ms in RGB8): before 2026-10-24 camera_lab left too little and 12-bit exposures longer than ≈ 124 ms were rejected (fixed) | [measured 2026-10-24] |
| short exposures under the floods | frame-to-frame brightness variation 6.5 % at 0.05 ms, 3.8 % at 0.1, 2.2 % at 0.25, 1.0 % at 0.5, **0.8 % at 1 ms**, 0.6 % from 5 ms (the floor). No row bands (global shutter): the bench allows 1 ms for global-shutter cameras | [measured 2026-10-24] |
| triggered acquisition | `TriggerOverlap` Off / Readout set how much exposure may overlap the previous readout | [manual] |
| trigger-to-image delay | measured **on the machine** with the production trigger; exact exposure start needs PTP time sync | [to measure on site] |
| new settings visible after | **the next frame** (0 stale frames after an exposure change) | [fingerprint 2026-10-24 16:15] |

## 6. Measured performance (RGB12V1Packed, master gain 0 dB, BlackLevel 0)
Full scale = 4095 DN. "Runs" = how many measurement runs the value rests on.

| property | value | fraction of full scale | runs / units | source |
|---|---|---|---|---|
| black level | **100 DN** warm (≈ 60 °C, spread 1.0 %); 96–101 DN while warming | 2.4 % | 9 / 1 | [fingerprints 2026-10-24/25] |
| read noise (dark, temporal) | **1.68–1.75 DN ≈ 1.9–2.0 e⁻** at 1 ms (1.8–1.9 at 10 ms, 2.4–2.9 at 100 ms, 7.7 at 1 s, 8.8 at 2 s) | 0.04 % | 12 / 1 (spread ≤ 0.54 %) | [fingerprints 2026-10-24/25] |
| row noise (temporal, dark) | 0.29 DN rms | | 1 / 1 | [fingerprint 2026-10-24] |
| DSNU | 3.6 DN at 1–10 ms, 7.5 DN at 100 ms, 20 at 500 ms, 37 at 1 s, 59 at 2 s | | 1 / 1 | [fingerprint 2026-10-25 14:30] |
| black vs exposure | 100 DN to 50 ms, then **falling**: 96 at 100 ms, 81 at 500 ms, 61 at 1 s, 34 at 2 s (the camera's black correction over-corrects long exposures) | | 1 / 1 | [fingerprint 2026-10-25 14:30] |
| system gain | **0.893 / 0.897 / 0.894 DN/e⁻** (three fingerprints) | | 9 / 1 (spread ≤ 0.26 %) | [fingerprints 2026-10-24/25] |
| full well | **4 470 / 4 458 / 4 470 e⁻** (the 12-bit ADC clips first at master 0 dB) | | 9 / 1 | [fingerprints 2026-10-24/25] |
| **dynamic range** (range ÷ read noise) | **67 dB (11.2 stops)** | | 3 / 1 | [fingerprint 2026-10-24] |
| saturation | 4094 DN | 100 % | | [fingerprint 2026-10-24] |
| dark current | **0 DN/s** up to 50 ms when warm and steady; beyond, the black level falls instead of rising (row above), so a single "dark current" does not describe it. The 40 DN/s of 2026-10-24 16:15 was the black drifting while the camera warmed (57 → 64 °C) | | 12 / 1 | [fingerprints 2026-10-24/25] |
| linearity error | **0.11 / 0.19 / 0.21 %** of range | | 9 / 1 | [fingerprints 2026-10-24/25] |
| peak SNR (flat, 90 % of full scale) | **64–65** (shot-noise limit √4470 = 67) | | 6 / 1 | [fingerprints 2026-10-24/25] |
| pixel response non-uniformity | **0.6–0.7 %**, equal in R, G and B | | 6 / 1 (spread ≤ 1.1 %) | [fingerprints 2026-10-24] |
| vignetting | lens-, iris- and light-dependent (§ 7): JAI 6 mm 0628-C3 corners 0.82 at f/2.8, 0.93 at f/4; the moved bench lights the diffuser's corners ≈ 5 % brighter (1.05 at f/5.6), so the lens alone is ≈ 0.78 at f/2.8 | | 3 / 1 each | [fingerprints 2026-10-24/25] |
| colour under the bench floods | R/G 1.03 / 0.84 / 1.27 and B/G 0.82 / 0.92 / 0.72 in three runs (each repeatable within 1 %): **a property of the light at the time and place, not the camera** — balance colour on site, under the production light | | 3 / 1 each | [fingerprints 2026-10-24/25] |
| hot pixels | unit-specific: **3 160–3 648** on U330439 (8 σ, 100 ms; fewer when warm and steady), most mild: 120 above 200 DN, 16 above 400 DN; ≈ 3 000 at every exposure to 1 s, then the uneven 2 s dark frame makes a quarter of all pixels count as hot | | 4 / 1 | [fingerprints 2026-10-24/25] |
| internal temperature | ≈ 60 °C streaming (57 → 64 °C while warming) | | | [fingerprints 2026-10-24/25] |
| quantization | 1 DN everywhere (the read noise, 1.74 DN, is above one step) | | | [fingerprint 2026-10-24] |

### SNR vs signal level [fingerprint 2026-10-24]
| signal above black | % of full scale | SNR |
|---|---|---|
| 84 DN | 2 % | 9.6 |
| 221 DN | 5 % | 15.7 |
| 475 DN | 12 % | 23.2 |
| 955 DN | 23 % | 32.9 |
| 1 596 DN | 39 % | 42.4 |
| 2 243 DN | 55 % | 50.1 |
| 2 888 DN | 71 % | 56.7 |
| **3 703 DN** | **90 %** | **64.8 (best)** |

- One simple noise model fits the whole range: **σ² = 1.74² + 0.893 · signal** (DN). There are no
  handovers or dips: SNR rises monotonically as √signal, so the brightest unclipped exposure is
  always the cleanest.
- An SNR of ≈ 65 means ≈ 1.5 % noise per pixel per frame at the top. Averaging N frames improves it
  by √N; measurements that need better per-pixel precision average frames or pixels.
- Colour planes at ≈ 50 % of full scale: SNR R 39–43, G 47, B 42–44 (the dimmer plane is noisier).

### Gain (master mode, constant output ≈ 1 000 DN) [fingerprint 2026-10-24 16:15]
| gain dB | DN/e⁻ | read noise e⁻ | full well e⁻ | SNR at 1 000 DN |
|---|---|---|---|---|
| 0 | 0.883 | 2.0 | 4 530 | 33.5 |
| 6 | 1.74 | 1.8 | 2 298 | 23.8 |
| 12 | 3.45 | 1.7 | 1 158 | 16.9 |
| 18 | 6.87 | 1.6 | 582 | 12.0 |

Repeated 2026-10-25 within 0.4 %; every 3 dB step measured on 2026-10-25 14:30 (18 dB in the
low-light row), read noise 2.14 → 1.7 e⁻ from 0 to 18 dB.

### Low light [fingerprint 2026-10-25 14:30]
Iris fully closed (light 0.021× fully open): measured SNR matches the noise model down to the
darkest signal tried — **3.9 DN: SNR 1.58 (model 1.53)**; 8.5 DN: 2.57 (2.60); 35 DN: 6.05
(6.02). No pixel is clipped at black. Averaging N frames improves SNR by exactly √N (16 frames:
× 3.99).

## 7. Colour: the prism
**Corner shading vs iris** (JAI 6 mm 0628-C3, diffuser, floods) [fingerprints 2026-10-24/25]:

| iris | corners / centre (R / G / B) | colour cast at the corners |
|---|---|---|
| f/2.8 (fully open) | 0.816 / 0.819 / 0.827 | **1.4 %** (blue corners slightly brighter) |
| f/4–f/5.6 | 1.05 (bench light uneven) | 0.3–0.6 % |
| f/8 | 1.04 | 0.3 % |

Opening wider than the prism's F4 rating adds vignetting (≈ 0.78 lens-only at f/2.8) and a
small colour shading (1.4 %): fine for detection, and a flat-field correction per colour plane
removes it for measurement. From f/4 down the colours shade alike.

Each colour has its own sensor and its own gain path, so the three planes are independent full-
resolution images: no demosaicing artefacts, no colour aliasing, and the noise is the same in
each plane (PRNU R 0.68 / G 0.69 / B 0.68 %). Colour is balanced on the PC (`raw` keeps the
camera's white balance at 1.0). The planes are only as well registered as the prism and lens
allow: a lens made for prism cameras (the JAI 0628-C3 is) keeps the colours aligned. The prism
is rated "F4 prism optical system" [manual]; at f/2.8 that costs 1.4 % of colour shading at the
corners (above). Prism alignment is assumed good (factory).

## 8. Limitations and known behaviours
- **Small full well:** 4 470 e⁻ at master 0 dB caps the SNR at ≈ 65 and the dynamic range at
  11 stops; individual gain mode at 0 dB doubles the full well to ≈ 9 460 e⁻ (§ 4; camera_lab
  stays in master). It is not an HDR camera.
- **Runs hot, drifts while warming:** ≈ 60 °C streaming; the black drifts ≈ 4 DN while it warms.
- **Iris wider than f/4:** more vignetting and 1.4 % colour shading at f/2.8 (§ 7).
- **GigE limits the frame rate:** 8 fps in 12-bit, 12 fps in 8-bit; the sensors could do 24 fps.
- **Only RGB12V1Packed is raw:** the 8/10-bit formats carry the camera's gamma 0.45 and white balance.
- **No boot-set selector:** once any user set is saved the camera boots from User Set 1.
- **`UserSetSave` needs the stream stopped.**
- **Long exposures (above ≈ 100 ms) are poor:** the black level falls (34 DN at 2 s instead of
  100), read noise rises to 8.8 DN and the dark frame becomes uneven (DSNU 59 DN at 2 s). Keep
  exposures at or below ≈ 100 ms, or subtract a dark frame taken at the same exposure and
  temperature.
- **Gain does not help a dim scene** (+12 % SNR at best, § 4).

## 9. Diagnostic signatures (what a misconfigured or failing camera looks like)
| signature | meaning |
|---|---|
| doubling exposure gives less than × 2 | not in RGB12V1Packed (gamma on) |
| colours look balanced in `raw` | white balance gains are not 1.0 |
| black far from 100 DN with the cap on | BlackLevel changed, or a fault / heat (read `DeviceTemperature`) |
| settings differ after power-up | normal once a user set was saved: it boots from User Set 1 |
| `UserSetSave` fails | the stream is running |
| red / blue fringes along edges | prism or lens misalignment, or a lens not made for prism cameras |
| frame rate below § 5 | exposure longer than one frame period, ROI/format, or network |
| hot-pixel count far above the unit's fingerprint | sensor ageing or damage |

## 10. Checking the camera's state
| test | method | expected |
|---|---|---|
| linear output | steady scene, exposure E and 2E (5–40 % of full scale) | signal × 1.95–2.05 |
| dark | lens cap on, 10 ms, gain 0, BlackLevel 0, camera warm (≈ 60 °C) | black ≈ 100 DN, read noise ≈ 1.8 DN |
| start-up | `two-d reset` / `reset_camera` | all `raw` settings read back equal |
| hot pixels | lens cap on, 100 ms | close to the unit's fingerprint count |

## 11. Open questions
- **Individual gain mode as the design mode:** one stop more headroom (≈ 71.6 vs 66.5 dB);
  kept on master for now (Derek 2026-10-25).
- **Per-sensor exposures in a user set:** not testable while camera_lab sets common exposure on
  every open.
- **Why the black level falls at long exposures** (the camera's black correction?), and whether
  a dark frame at the same exposure fully removes it.
- Temperature beyond the bench's ≈ 60 °C steady state (a hot enclosure on site): not measured.
- Why the bench light's colour changed between runs (other lights on? daylight?).

## 12. Test conditions and sources
- Bench: white board lit by the DC LED floods (full), opal acrylic diffuser on the lens, JAI 6 mm
  f/2.8 JVS-C118-0628-C3 (prism lens) at about f/4 (morning) and between f/4 and f/5.6
  (afternoon, after the bench moved), fully open f/2.8 with the 1 ms global-shutter minimum and
  f/5.6 for the lens row (2026-10-25, camera warm), lens cap for darks. Unit U330439, firmware 0.0.3.4 (device version 0.1.0.7).
- Camera profile: `config/camera_profiles/U330439.yaml`
- Fingerprints: `WS/config/camera_fingerprints/U330439/`
- JAI AP-1600T/3200T-PGE user manual v2.2: `Manuals/vision/jai_apex/`

## 13. Measured data (generated from the catalog)
Every number below is from the catalog (`camera-lab two-d catalog runs`).

| run | kind | lens | iris | °C | used |
|---|---|---|---|---|---|
| U330439/20261024T131257Z | fingerprint | jai_6mm_0628 | - | - | yes |
| U330439/20261024T161546Z | fingerprint | jai_6mm_0628 | - | 57.4–64.4 | yes |
| U330439/20261024T170000Z-live | live_check | jai_6mm_0628 | f/2.8 | - | yes |
| U330439/20261025T101320Z | fingerprint | jai_6mm_0628 | f/2.8 | 59.8–61.2 | yes |
| U330439/20261025T143049Z | fingerprint | jai_6mm_0628 | f/2.8 | 61.8–63.9 | yes |

### Headline numbers per fingerprint

| run | black DN | read noise DN | row noise DN | dark current DN/s | DN/e- | full well e- | linearity % | PRNU % |
|---|---|---|---|---|---|---|---|---|
| 20261024T131257Z | 100.1 | 1.741 | 0.293 | 0 | 0.8932 | 4470 | 0.114 | 0.692 |
| 20261024T161546Z | 95.91 | 1.684 | 0.3173 | 40.5 | 0.8966 | 4458 | 0.1914 | 0.5939 |
| 20261025T101320Z | 100 | 1.728 | 0.3085 | 0 | 0.8935 | 4470 | 0.2086 | 0.6809 |
| 20261025T143049Z | 100 | 1.751 | 0.3113 | 0 | 0.8933 | 4471 | 0.1366 | 0.6823 |

### Darks per exposure (U330439/20261025T143049Z)

| exposure ms | black DN | read noise DN | hot pixels |
|---|---|---|---|
| 1 | 100 | 1.751 | 3210 |
| 2 | 100.1 | 1.762 | 3278 |
| 5 | 100.3 | 1.816 | 3323 |
| 10 | 101.6 | 1.888 | 3719 |
| 20 | 101.3 | 2.029 | 2880 |
| 50 | 102.2 | 2.401 | 3044 |
| 100 | 96.28 | 2.935 | 3225 |
| 200 | 93.11 | 3.756 | 3099 |
| 500 | 81.24 | 5.548 | 3018 |
| 1000 | 61.15 | 7.665 | 2917 |
| 2000 | 34.37 | 8.776 | 7.506e+05 |

### Noise vs gain (U330439/20261025T143049Z)

| gain mode | gain dB | SNR | DN/e- | read noise e- | full well e- |
|---|---|---|---|---|---|
| individual | 0 | 33.2 | 0.4224 | 2.49 | 9458 |
| master | 0 | 33.5 | 0.8818 | 2.14 | 4530 |
| master | 3 | 28.2 | 1.241 | 2.04 | 3218 |
| master | 6 | 23.8 | 1.744 | 1.95 | 2290 |
| master | 9 | 20 | 2.454 | 1.89 | 1628 |
| master | 12 | 16.9 | 3.441 | 1.84 | 1161 |
| master | 15 | 14.2 | 4.852 | 1.79 | 823 |
| master | 18 | 11.7 | 7.146 | 1.7 | 559 |

### Low light (U330439/20261025T143049Z)

| signal DN | SNR | model SNR |
|---|---|---|
| 3.917 | 1.58 | 1.53 |
| 8.473 | 2.57 | 2.6 |
| 17.55 | 4.05 | 4.05 |
| 35.49 | 6.05 | 6.02 |

### What a user set keeps (U330439/20261025T143049Z)

| setting | after a restart | how |
|---|---|---|
| pixel_format | kept | software reboot, UserSet1 |
| roi_width | kept | software reboot, UserSet1 |
| roi_height | kept | software reboot, UserSet1 |
| roi_x | kept | software reboot, UserSet1 |
| roi_y | kept | software reboot, UserSet1 |
| exposure_us | kept | software reboot, UserSet1 |
| gain_db | kept | software reboot, UserSet1 |
| gamma | kept | software reboot, UserSet1 |
| wb_red | kept | software reboot, UserSet1 |
| wb_blue | kept | software reboot, UserSet1 |

### Lenses tested on this camera

- `jai_6mm_0628`: see `lenses/jai_6mm_0628.md`

---
Generated from `WS/config/camera_catalog.sqlite`: edit the catalog (`camera-lab two-d catalog ...`), never this file.
