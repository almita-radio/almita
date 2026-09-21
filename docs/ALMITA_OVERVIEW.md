<p align="center">
  <img src="../almita-logo.png" alt="ALMITA logo" width="100%">
</p>

# ALMITA

**Antenna Listening Mostly to Interference, Tentatively Astronomy**

ALMITA is an open-source amateur 21 cm neutral hydrogen radio telescope developed in **Chile** by **Felipe Fridman G.**  
Contact: **ffridman@gmail.com**

It combines a Raspberry Pi 5, RTL-SDR receivers, an equatorial mount controlled through INDI/OnStep, automated observing, reproducible reduction and science pipelines, and a local web interface designed to work completely offline in the field.

ALMITA is mostly vibe-coded, extensively tested, occasionally threatened with a hammer, and still held together in suspiciously many places by **plastic cable ties**.

This is considered temporary.

It has been considered temporary for quite some time.

---

## What ALMITA does

ALMITA can:

- Generate sky mosaics from beam width, sampling and angular field size.
- Plan observations in equatorial, rotated and Galactic geometries.
- Control an equatorial mount through **INDI + OnStep**.
- Acquire raw I/Q from an **RTL-SDR Blog V4**.
- Store observations and metadata in **HDF5**.
- Record pointing, time, observatory coordinates and receiver temperatures.
- Characterize receiver gain and freeze a validated nominal operating point for each science session.
- Apply compatible relative calibration profiles with explicit provenance.
- Reject or downgrade incompatible calibration instead of silently forcing it.
- Generate live spectrum, waterfall and sky-map quicklooks.
- Monitor sessions through a local web console.
- Operate completely offline in the field.
- Preserve RAW observations as immutable evidence.
- Reduce RAW captures into reproducible Level 1 spectra.
- Build beam-aware spectral cubes and maps from reduced data.
- Monitor the local RF environment through an independent secondary receiver.
- Run mount, SDR and system preflight/bench checks before field observing.
- Replay and validate processing products without reacquiring the sky.

The general data flow is:

```text
OBSERVE
   ↓
RAW I/Q + metadata
   ↓
REDUCE
   ↓
Level 1 spectra
   ↓
SCIENCE
   ↓
cubes, integrated maps and velocity products
```

ALMITA currently treats HI intensity as **relative/instrumental data**.

Absolute calibration in Kelvin is a future capability, not something the telescope pretends to already have.

---

## Hardware

The current system is built around:

- **Raspberry Pi 5**
- **RTL-SDR Blog V4** — primary science receiver
- **RTL-SDR Blog V3** — secondary RFI reference receiver
- **Nooelec Hydrogen LNA** — primary 1420 MHz LNA
- **RTL-SDR.com Wideband LNA** — RFI reference chain
- **Nooelec Flamingo FM** — FM broadcast notch filter for RFI monitoring
- 1420 MHz feed
- Modified ~90 × 60 cm grid/parabolic reflector
- Equatorial mount
- **OnStep** controller
- **INDI**
- Temperature monitoring for the RF hardware
- Independent reference antenna for RFI monitoring

The hardware is intentionally accessible, experimental and repairable.

It is not a commercial telescope kit.

Some parts are carefully engineered.

Some parts are carefully attached with zip ties.

Both approaches have survived surprisingly well so far.

---

## Main receiver and RFI reference

ALMITA keeps the astronomical receiver and the local interference monitor as two independent chains.

### MAIN — science receiver

```text
1420 MHz feed
      │
      ▼
Nooelec Hydrogen LNA
      │
      ▼
RTL-SDR Blog V4
      │
      ▼
rtl_tcp
      │
      ▼
OBSERVE
      │
      ▼
RAW HDF5 → REDUCE → SCIENCE
```

Typical observing configuration:

- Center frequency: **1420.405752 MHz**
- Sample rate: **2.4 MS/s**
- Gain: **40.2 dB**
- Bias-T: **ON**

The nominal gain is not chosen arbitrarily.

It is characterized experimentally, validated before observing and then kept fixed throughout a science observation.

### RFI_REF — interference reference

```text
Reference antenna
      │
      ▼
FM broadcast notch filter
      │
      ▼
Wideband LNA
      │
      ▼
Secondary RTL-SDR
      │
      ▼
rtl_tcp
      │
      ▼
RFI diagnostics
```

RFI_REF does **not** replace or modify the MAIN science signal.

Its job is to provide an independent view of the local RF environment so that suspicious features can be compared against a separate receiver path.

The science receiver remains authoritative, and loss of the reference receiver must not corrupt the primary observation.

---

## Calibration

ALMITA separates **receiver operating-point characterization** from **scientific calibration**.

They are related, but they are not the same thing.

### Gain characterization

ALMITA does **not** use AGC during science observations.

Before normal observing, the receiver chain is characterized across multiple gain settings while measuring things such as:

- ADC headroom
- clipping
- RMS level
- noise floor
- stability
- repeatability
- response to strong local RF

The goal is to identify a nominal gain that provides useful sensitivity without approaching clipping, compression or non-linear behavior.

Once that operating point has been established:

1. a short RF preflight validates it before the session;
2. if conditions are safe, the nominal gain is kept;
3. once science acquisition starts, gain remains fixed across the observation.

This makes comparisons between pointings and sessions much more defensible.

Changing gain halfway through a sky map would be scientifically inconvenient and, more importantly, very rude.

### Relative calibration

ALMITA currently supports **relative calibration**.

REDUCE can apply a compatible calibration profile and records:

- calibration level
- calibration profile identity
- calibration profile hash
- compatibility state
- provenance back to the RAW observation

If a calibration profile is incompatible with the current acquisition settings, it is not silently forced onto the data.

The product falls back to an uncalibrated state and records why.

Current science products therefore remain in **relative/instrumental units**.

### Absolute calibration

Absolute brightness-temperature calibration in Kelvin is a future capability.

The intended path includes controlled reference measurements and a physically defensible calibration model for the complete RF chain.

Until that work is complete, ALMITA does not present its intensity scale as Kelvin, Jansky or any other absolute physical unit.

A relative measurement that says exactly what it is is more useful than an absolute-looking number with imaginary confidence.

---

## Observation planning

ALMITA does not define a grid as “some number of points that looks nice”.

The observing geometry is derived from:

- beam width
- sampling factor
- field size
- sky position
- coordinate frame
- observation mode

Supported planning includes equatorial rectangular and rotated fields, with Galactic geometry also available for survey-style work.

Planning products can show the same observation in:

- Galactic coordinates
- Equatorial coordinates
- Local observer geometry

A local HI sky dataset can be used as **context** during planning, but contextual sky data are always kept separate from measured ALMITA data.


---

## Alignment and pointing

Alignment is deliberately conservative.

ALMITA does not issue arbitrary SYNC commands just because the mount appears slightly wrong.

A pointing correction needs a defensible physical reference.

The system therefore prefers:

> an honest pointing uncertainty over fake precision.

Alignment, normal observing and mount validation are separate operations so that corrections remain explicit and auditable.

The physical antenna beam is also still considered **provisional**.

Its configured beam width is useful for planning and gridding, but it is not presented as a measured final FWHM.

A dedicated beam-characterization campaign is planned.

---

## REDUCE V1

**REDUCE V1 is frozen.**

It converts immutable RAW observations into self-describing **Level 1 MasterSpectrum** products.

The pipeline includes:

```text
ingest
→ spectral estimate
→ masking
→ calibration
→ baseline handling
→ RFI reference handling
→ velocity conversion
→ resampling
→ averaging
→ quality assessment
→ persistence
```

Important properties include:

- deterministic replay
- explicit provenance back to RAW captures
- relative or uncalibrated intensity only
- topocentric frequency axis
- LSRK velocity axis when sufficient metadata exist
- propagated uncertainty
- masks and quality states
- self-describing HDF5 products
- completely offline operation

REDUCE does **not** create sky maps or reinterpret RAW data during later science processing.

That boundary is intentional.

---

## SCIENCE V1

**SCIENCE V1 is also frozen.**

SCIENCE consumes **REDUCE Level 1 only**.

It never goes back to RAW I/Q to redo FFTs, calibration or Doppler correction.

The current pipeline can generate:

- beam-aware spectral cubes
- integrated relative-intensity maps
- velocity-centroid maps
- velocity-dispersion maps
- coverage and quality products
- deterministic replay and comparison products

The cube uses explicit beam weighting and propagated statistical uncertainty.

The beam is currently operator-configured rather than physically measured, and the uncertainty model is statistical rather than a complete description of every systematic effect.

Those limitations are recorded rather than hidden.

No Kelvin.

No magical precision.

No beautifying maps until they look scientifically convenient.

---

## Feature forensics

During validation, ALMITA found a narrow spectral feature in one real campaign that moved strongly with observation order.

Rather than naming it prematurely, a separate forensic workflow was built to ask a simpler question:

> Is a feature fixed on the sky, fixed in receiver frequency, drifting with time, or entangled with the scan geometry?

The forensic tools can compare:

- LSRK velocity
- topocentric frequency
- time
- sky position
- global spectrum shifts
- scan-order effects

Dedicated follow-up experiment designs include repeated fixed-sky observations and bracketed A/B/C sequences that deliberately break time-versus-position correlation.

The current classification of the original feature remains appropriately boring:

**unresolved; instrument/RFI follow-up warranted.**

That is preferable to inventing an astrophysical story.

---

## Bench and field validation

ALMITA includes dedicated validation tooling before a field campaign.

The indoor bench can verify, without producing science data:

- repository and configuration integrity
- clock sanity
- INDI connectivity and mount state
- forbidden mount commands
- SDR identity and configuration
- SDR throughput
- clipping and constant-block detection
- socket gaps
- memory and storage availability

A real MAIN receiver bench at 2.4 MS/s sustained approximately the expected **4.8 MB/s I/Q stream** after a reader bug was found and corrected.

The bench is deliberately labeled:

**BENCH DATA — NOT SCIENCE**

Outdoor execution has a separate preflight and explicit operator confirmation.

A bench PASS means the equipment and software are behaving coherently.

It does not mean the sky has already agreed to cooperate.

---

## Web console

ALMITA has a local web interface intended for field use.

It includes views for:

- observation
- alignment
- calibration
- system status
- progress and quicklook products

The web layer exposes operational health separately from process health, protects observation start paths, survives backend reloads, and avoids pretending that missing or stale telemetry is valid data.

Everything remains local.

No cloud connection is required to point the antenna, acquire data or process an observation.

---

## Reproducibility

A recurring design rule in ALMITA is:

> RAW is evidence.

Original captures are preserved.

Processing produces new products rather than rewriting observations.

Important outputs carry provenance such as:

- campaign identity
- point number
- timestamps
- receiver configuration
- coordinates
- calibration information
- software/schema versions
- source hashes
- processing-session identity

REDUCE and SCIENCE can be replayed and compared deterministically.

This is slightly less exciting than discovering aliens, but substantially more useful.

---

## Software stack

ALMITA is mainly Python running locally on Linux.

Core technologies include:

- Python
- NumPy
- Astropy
- h5py / HDF5
- INDI / PyINDI
- OnStep
- rtl_tcp
- Matplotlib
- systemd
- local HTTP services

Observation and scientific processing are designed to work without Internet access.

---

## Project status

The main acquisition, reduction and science architectures are operational.

REDUCE V1 and SCIENCE V1 have frozen contracts, and the web/bench/field tooling has matured enough that new work is increasingly driven by **real field evidence rather than architecture invention**.

Current areas of work include:

- physical beam characterization
- absolute calibration in Kelvin
- improved receiver calibration models
- RFI-reference field validation
- forensic follow-up observations
- expanded Galactic survey modes
- continued outdoor end-to-end validation

ALMITA is still an amateur instrument.

That does not mean its data handling has to be amateurish.

---

## Philosophy

ALMITA is an experiment in how far inexpensive, accessible hardware can be pushed when combined with careful software, reproducible processing and a refusal to silently hide inconvenient results.

If a measurement is provisional, it says so.

If calibration is relative, it says so.

If a feature is unexplained, it stays unexplained.

And if something can be fixed with a plastic cable tie...

there is a non-zero probability that it already has been.
