% OCTAVE_BAND_SPL  A- and Z-weighted broadband and full-octave Leq from a digital microphone WAV.
% Run the script, or edit these settings first. Requires Signal Processing Toolbox.
% The sensitivity reference is a 1 kHz, 1 Pa RMS sine (about 94 dB SPL).
% 0 dBFS denotes a full-scale sine with normalized peak amplitude 1.
% Use the original PCM recording, with known, fixed gain and no normalization.

%% Inputs
wavFile = "";              % Empty opens a WAV file picker.
micSensitivity_dBFS = -36; % IM72D128V01 typical (+20dB gain added later). Scalar or one value per channel.
captureGain_dB = [];       % Empty reads adjacent nRF54 JSON, or asks for gain.
% The JSON stores the programmed gain relative to the PDM reset setting.
% Automatic mode assumes unity at reset and no further scaling on export.
% For a known offset, enter the total microphone-output-to-WAV gain here.
% If sensitivity already includes all gain through the WAV, set captureGain_dB = 0.
taper_ms = 5;             % Half-cosine fade per end, in milliseconds. Zero disables it.

if strlength(wavFile) == 0
    [name, folder] = uigetfile('*.wav', 'Select microphone WAV');
    if isequal(name, 0), return; end
    wavFile = fullfile(folder, name);
end
[x, fs] = audioread(wavFile);
[n, channels] = size(x);
assert(n >= 2 && all(isfinite(x(:))), 'Expected at least two finite WAV samples per channel.');
info = audioinfo(wavFile);
if any(x(:) >= 1 - 2^(1-info.BitsPerSample) | x(:) <= -1)
    warning('WAV reaches a digital rail. Clipping can bias the band levels.');
end
if isempty(captureGain_dB)
    [folder, stem] = fileparts(wavFile);
    sidecar = fullfile(folder, strcat(stem, '.json'));
    if isfile(sidecar)
        meta = jsondecode(fileread(sidecar));
        if isfield(meta, 'gain_millidb')
            assert((~isfield(meta,'sample_rate_hz') || meta.sample_rate_hz == fs) && ...
                (~isfield(meta,'sample_count') || meta.sample_count == n), ...
                'The JSON metadata does not match this WAV. Set captureGain_dB manually.');
            captureGain_dB = meta.gain_millidb / 1000;
        end
    end
    if isempty(captureGain_dB)
        captureGain_dB = str2double(input('Applied gain in dB (enter 0 for unity gain): ', 's'));
    end
end
for value = {micSensitivity_dBFS, captureGain_dB}
    validateattributes(value{1}, {'numeric'}, {'real','finite','vector','nonempty'});
    assert(isscalar(value{1}) || numel(value{1}) == channels, ...
        'Supply one shared sensitivity/gain or one value per WAV channel.');
end
sensitivity = reshape(micSensitivity_dBFS, 1, []);
gain = reshape(captureGain_dB, 1, []);
% A 1 Pa RMS sine produces 10^((sensitivity+gain)/20) peak WAV units.
paPerWavUnit = sqrt(2) .* 10.^(-(sensitivity + gain)/20);
pressure = (x - mean(x, 1)) .* paPerWavUnit; % Remove recorder DC before filter startup.
validateattributes(taper_ms, {'numeric'}, {'real','finite','scalar','nonnegative'});
fadeIntervals = min(round(fs*taper_ms/1000), floor((n-1)/2));
if fadeIntervals > 0
    idx = (1:fadeIntervals+1)';
    fade = (1 - cos(pi*(idx-1)/fadeIntervals))/2;
    pressure(idx,:) = pressure(idx,:) .* fade;
    pressure(n+1-idx,:) = pressure(n+1-idx,:) .* fade;
    fprintf('Applied %.3g ms half-cosine fade per end. Leq includes both fades.\n', ...
        1000*fadeIntervals/fs);
end

%% Broadband levels over the tapered recording
% Apply the A-weighting magnitude to each DFT line of this finite record.
% Fold negative frequencies for a real, zero-phase offline weighting.
f = (0:n-1)' * (fs/n);
f = min(f, fs-f);
aResponse = @(hz) (hz.^2 ./ (hz.^2 + 20.598997^2)) .* ...
    (hz ./ hypot(hz,107.65265)) .* (hz ./ hypot(hz,737.86223)) .* ...
    (12194.217^2 ./ (hz.^2 + 12194.217^2));
aPressure = ifft(fft(pressure,[],1) .* (aResponse(f)/aResponse(1000)), ...
    [],1,'symmetric');
refPa2 = (20e-6)^2;
broadbandLevels = table((1:channels)', ...
    (10*log10(mean(aPressure.^2,1)/refPa2))', ...
    (10*log10(mean(pressure.^2,1)/refPa2))', ...
    'VariableNames', {'Channel','LAeq_dBA','LZeq_dBZ'});

%% Octave levels over the original recording duration
G = 10^(3/10);
nominalHz = [31.5 63 125 250 500 1000 2000 4000 8000 16000]';
fc = 1000 * G.^(-5:4)';
lowerHz = fc / sqrt(G);
upperHz = fc * sqrt(G);
order = 10;
levels = nan(numel(fc), channels);
aLevels = levels;
margin = nan(size(fc));
bwError = nan(size(fc));
status = repmat("Incomplete band at WAV sample rate", size(fc));
% poctave can truncate high bands near Nyquist. Interpolate only for these
% bands. interpft preserves the recorded DFT lines and introduces no new
% measured bandwidth. Both completeness and bandwidth use the original fs.
complete = upperHz < fs/2;
assert(any(complete), 'This sample rate cannot represent the 31.5 Hz octave band.');
high = upperHz > fs/4;
for factor = [1 2]
    rows = find(complete & (high == (factor == 2)));
    if isempty(rows), continue; end
    rate = factor * fs;
    signalPa = [pressure, aPressure];
    if factor == 2, signalPa = interpft(signalPa, 2*n, 1); end
    limits = [0.99*fc(rows(1)), 1.01*fc(rows(end))];
    [powerPa2, actualHz] = poctave(signalPa, rate, 'power', 'BandsPerOctave', 1, ...
        'FilterOrder', order, 'FrequencyLimits', limits, 'Weighting', 'none');
    assert(numel(rows) == numel(actualHz) && all(abs(actualHz(:)./fc(rows)-1) < 1e-9), ...
        'Unexpected center frequencies from poctave.');
    powerPa2(powerPa2 <= eps(0)) = 0; % poctave may floor exact silence at eps(0).
    levels(rows,:) = 10*log10(powerPa2(:,1:channels) / refPa2);
    aLevels(rows,:) = 10*log10(powerPa2(:,channels+1:end) / refPa2);
    % In R2025b this is the actual SOS designer used by poctave. If a new
    % release removes it, keep the levels and mark the checks unavailable.
    status(rows) = "Response check unavailable";
    if isempty(which('signal.internal.octave.designOctaveBank')), continue; end
    [filters, checkedHz] = signal.internal.octave.designOctaveBank(order, limits, 1, rate);
    assert(isequal(checkedHz(:), actualHz(:)), 'Filter check and poctave centers differ.');
    for j = 1:numel(rows)
        i = rows(j);
        [margin(i), bwError(i)] = checkClass2(filters{j}, fc(i), rate, fs/2, G);
        if margin(i) >= 0 && abs(bwError(i)) <= 0.6
            status(i) = "Passed digital response checks";
        else
            status(i) = "Failed digital response checks";
        end
    end
end
filterChecks = table(nominalHz, fc, lowerHz, upperHz, margin, bwError, status, ...
    'VariableNames', {'Nominal_Hz','Center_Hz','Lower_Hz','Upper_Hz', ...
    'MinMaskMargin_dB','BandwidthDeviation_dB','Status'});
bands = numel(fc);
channel = repelem(1:channels, bands);
octaveLevels = table(channel(:), repmat(nominalHz, channels, 1), ...
    repmat(fc, channels, 1), levels(:), repmat(status, channels, 1), ...
    'VariableNames', {'Channel','Nominal_Hz','Center_Hz','Leq_dBSPL','FilterStatus'});
octaveLevels.LZeq_dBZ = octaveLevels.Leq_dBSPL; % Retain original column for existing callers.
octaveLevels.LAeq_dBA = aLevels(:);
fprintf('A- and Z-weighted Leq over %.6g s. Sensitivity %s dBFS at 1 Pa, capture gain %s dB.\n', ...
    n/fs, mat2str(sensitivity), mat2str(gain));
disp(broadbandLevels);
disp(octaveLevels);

function [margin, bandwidthDeviation] = checkClass2(sos, fc, rate, nyquist, G)
    % IEC 61260-1:2014 Table 1, Eq. (11), (14)-(16). Reference attenuation
    % is 0 dB. Check both one-sided edge limits and the sampled response
    % over the WAV bandwidth. This checks the digital filter only.
    edges = [0 1/8 1/4 3/8 1/2 1 2 3 4]';
    top = log(nyquist/fc)/log(G);
    u = unique([linspace(-16,top,8193)'; -edges; edges; top]);
    u = u(u <= top);
    f = fc * G.^u;
    mag = ones(size(f));
    for k = 1:size(sos,1)
        h = freqz(sos(k,1:3), sos(k,4:6), f, rate);
        mag = mag .* abs(h);
    end
    att = -20*log10(max(mag, realmin));
    a = abs(u);
    inside = a <= 0.5;
    outside = a >= 0.5;
    maxAtt = interp1(edges(1:5), [0.6 0.7 0.9 1.7 5.8], a(inside));
    minAtt = interp1(edges(5:end), [0.8 15.6 39.5 54 60], min(a(outside),4));
    margin = min([att(inside)+0.6; maxAtt-att(inside); att(outside)-minAtt]);
    bandwidthDeviation = 10*log10(trapz(u, mag.^2)); % d(ln f) = ln(G) du
end
