/** Real browser PCM audio. Cloud keys stay in the FastAPI server. */

class PCM16Resampler {
  constructor(inputRate, outputRate = 16000) {
    this.ratio = inputRate / outputRate;
    this.tail = new Float32Array(0);
    this.position = 0;
  }

  process(input) {
    const samples = new Float32Array(this.tail.length + input.length);
    samples.set(this.tail);
    samples.set(input, this.tail.length);
    const output = [];
    while (this.position + 1 < samples.length) {
      const at = Math.floor(this.position);
      const mix = this.position - at;
      const sample = samples[at] * (1 - mix) + samples[at + 1] * mix;
      output.push(Math.round(Math.max(-1, Math.min(1, sample)) * (sample < 0 ? 32768 : 32767)));
      this.position += this.ratio;
    }
    const consumed = Math.min(Math.floor(this.position), samples.length);
    this.tail = samples.slice(consumed);
    this.position -= consumed;
    const bytes = new ArrayBuffer(output.length * 2);
    const view = new DataView(bytes);
    output.forEach((sample, index) => view.setInt16(index * 2, sample, true));
    return bytes;
  }
}

// A synthesis request has no AudioContext and cannot play by itself. At most
// one request ahead of the active sentence may hold PCM in this buffer.
class TTSSource {
  constructor(url, text) {
    this.text = text;
    this.startedAt = performance.now();
    this.firstPCMAt = null;
    this.frames = [];
    this.bufferedBytes = 0;
    this.listener = null;
    this.cancelled = false;
    this.complete = false;
    this.failed = false;
    this.socket = new WebSocket(url);
    this.socket.binaryType = 'arraybuffer';
    this.timer = setTimeout(() => this.fail('语音合成等待超时。'), 60000);
    this.socket.onopen = () => {
      if (!this.cancelled) this.socket.send(JSON.stringify({type: 'text', text}));
    };
    this.socket.onerror = () => this.fail('语音播放连接失败。');
    this.socket.onclose = () => {
      if (!this.cancelled && !this.complete && !this.failed) this.fail('语音生成提前中断，请重新播放。');
    };
    this.socket.onmessage = event => {
      if (this.cancelled || this.failed) return;
      if (event.data instanceof ArrayBuffer) {
        if (this.firstPCMAt === null) this.firstPCMAt = performance.now();
      } else {
        let data;
        try { data = JSON.parse(event.data); } catch { this.fail('语音服务返回无效事件。'); return; }
        if (data.type === 'error') {this.fail(data.message || '语音合成失败。'); return;}
        if (data.type === 'done') {this.complete = true; clearTimeout(this.timer);}
      }
      this.dispatch(event);
    };
  }

  dispatch(event) {
    if (this.cancelled) return;
    if (this.listener) {this.listener(event); return;}
    if (event.data instanceof ArrayBuffer) {
      this.bufferedBytes += event.data.byteLength;
      if (this.bufferedBytes > 2 * 1024 * 1024) {this.fail('下一句语音预取超过 2MB，已停止。'); return;}
    }
    this.frames.push(event);
  }

  attach(listener) {
    this.listener = listener;
    const frames = this.frames;
    this.frames = [];
    this.bufferedBytes = 0;
    for (const event of frames) {
      if (this.cancelled || this.listener !== listener) break;
      listener(event);
    }
  }

  stopSocket() {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({type: 'cancel'}));
    this.socket.close();
  }

  fail(message) {
    if (this.cancelled || this.failed) return;
    this.failed = true;
    clearTimeout(this.timer);
    this.frames = [];
    this.bufferedBytes = 0;
    this.stopSocket();
    this.dispatch({data: JSON.stringify({type: 'error', message})});
  }

  cancel() {
    if (this.cancelled) return;
    this.cancelled = true;
    clearTimeout(this.timer);
    this.frames = [];
    this.bufferedBytes = 0;
    this.listener = null;
    this.stopSocket();
  }
}

export class VoiceIO {
  constructor(options = {}) {
    this.options = options;
    this.context = null;
    this.micSocket = null;
    this.ttsSocket = null;
    this.stream = null;
    this.processor = null;
    this.micSource = null;
    this.muteGain = null;
    this.sources = new Set();
    this.nextPlayTime = 0;
    this.speechGeneration = 0;
    this.micGeneration = 0;
    this.energySince = 0;
    this.lastEnergyAt = 0;
    this.lastSpeechAt = 0;
    this.speechActive = false;
    this.finishSpeaking = null;
    this.metrics = {};
    this.currentSpeechText = '';
    this.activeTTS = null;
    this.prefetchedTTS = null;
    this.speechQueue = [];
    this.queueRunning = false;
    this.finishMicDrain = null;
    this.micDrainPromise = null;
  }

  emit(name, value) {
    if (typeof this.options[name] === 'function') this.options[name](value);
  }

  socketURL(path) {
    const url = new URL(path, this.options.baseURL || window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.href;
  }

  async ensureAudio() {
    if (!this.context || this.context.state === 'closed') {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) throw new Error('此浏览器不支持实时音频，请使用最新版 Chrome 或 Safari。');
      this.context = new Audio({latencyHint: 'interactive'});
    }
    if (this.context.state === 'suspended') await this.context.resume();
    return this.context;
  }

  get isSpeaking() {
    return this.sources.size > 0 || this.ttsSocket !== null;
  }

  get isOutputActive() {
    if (!this.context || this.context.state !== 'running') return false;
    const now = this.context.currentTime;
    return [...this.sources].some(source => source.playbackStart <= now && source.playbackEnd > now);
  }

  get isListening() {
    return this.stream !== null;
  }

  async startMic() {
    if (this.stream || this.micStarting) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('麦克风需要 HTTPS 或 localhost；请通过本机 localhost 地址打开。');
    }
    this.stopMic({discard: true});
    this.micStarting = true;
    const generation = ++this.micGeneration;
    let openedSocket;
    try {
      const context = await this.ensureAudio();
      const stream = await navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      }, video: false});
      if (generation !== this.micGeneration) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream;
      const socket = new WebSocket(this.socketURL('/ws/asr'));
      this.micSocket = openedSocket = socket;
      const ready = new Promise((resolve, reject) => {
        let settled = false;
        const settle = (fn, value) => { if (!settled) {settled = true; clearTimeout(timer); fn(value);} };
        const timer = setTimeout(() => settle(reject, new Error('语音识别连接超时。')), 25000);
        socket.onopen = () => socket.send(JSON.stringify({type: 'start', silenceMs: this.options.silenceMs || 1100}));
        socket.onerror = () => settle(reject, new Error('语音识别连接失败。'));
        socket.onclose = () => {
          settle(reject, new Error('语音识别连接已关闭。'));
          if (this.micSocket === socket && this.finishMicDrain) {this.finishMicDrain(); return;}
          if (this.micSocket === socket) {
            this.releaseMic();
            this.micSocket = null;
            this.emit('onState', {mic: false});
          }
        };
        socket.onmessage = event => {
          if (generation !== this.micGeneration) return;
          let data;
          try { data = JSON.parse(event.data); } catch { return; }
          if (data.type === 'ready') {
            settle(resolve, data);
            this.emit('onMetrics', {asrConnectionMs: data.connectionMs, asrReservationSeconds: 30,
              asrReservationPolicy: '每 30 秒预留；关闭麦克风后未使用的预留不返还'});
          } else if (data.type === 'partial' || data.type === 'final') {
            this.handleRecognition(data);
          } else if (data.type === 'done') {
            this.finishMicDrain?.();
          } else if (data.type === 'error') {
            const error = new Error(data.message || '语音识别失败。');
            settle(reject, error);
            this.emit('onError', error);
            this.stopMic({discard: true});
          }
        };
      });
      await ready;
      if (generation !== this.micGeneration || this.stream !== stream) return;
      const resampler = new PCM16Resampler(context.sampleRate);
      const source = context.createMediaStreamSource(stream);
      // ScriptProcessor is kept as a dependency-free fallback, including Safari.
      // At 48 kHz, 2048 samples add ~43 ms capture buffering, not a turn timeout.
      const processor = context.createScriptProcessor(2048, 1, 1);
      const mute = context.createGain();
      mute.gain.value = 0;
      this.micSource = source;
      this.processor = processor;
      this.muteGain = mute;
      processor.onaudioprocess = event => {
        const input = event.inputBuffer.getChannelData(0);
        this.observeEnergy(input);
        if (socket.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 256000) {
          this.emit('onError', new Error('麦克风连接积压，已停止；请检查网络后重新开麦。'));
          this.stopMic({discard: true});
          return;
        }
        const pcm = resampler.process(input);
        if (pcm.byteLength) socket.send(pcm);
      };
      source.connect(processor);
      processor.connect(mute);
      mute.connect(context.destination);
      this.emit('onState', {mic: true});
    } catch (error) {
      if (generation !== this.micGeneration) return;
      if (this.micSocket === openedSocket || !openedSocket) this.stopMic({discard: true});
      throw error;
    } finally {
      if (generation === this.micGeneration) this.micStarting = false;
    }
  }

  observeEnergy(input) {
    let power = 0;
    for (let i = 0; i < input.length; i++) power += input[i] * input[i];
    const rms = Math.sqrt(power / input.length);
    const now = performance.now();
    const threshold = this.options.energyThreshold || (this.isSpeaking ? 0.022 : 0.012);
    if (rms > threshold) {
      if (!this.energySince) this.energySince = now;
      this.lastEnergyAt = now;
      if (now - this.energySince >= 110 && !this.isSpeaking) this.signalSpeech('microphone');
    } else {
      this.energySince = 0;
      if (now - this.lastEnergyAt > 650) this.speechActive = false;
    }
  }

  hasLocalSpeechEnergy() {
    const now = performance.now();
    return Boolean(this.energySince && now - this.energySince >= 100 && now - this.lastEnergyAt < 700);
  }

  isCorrection(text) {
    return /不对|不是这样|等一下|等等|换个角度|先别|不要说|打断|\bstop\b|\bwait\b/i.test(text)
      || /(?:^|[，,。.!！\s])停(?:下|一下|止)?(?:[，,。.!！\s]|$)/.test(text);
  }

  isBackchannel(text) {
    const value = text.toLowerCase().replace(/[\s\p{P}\p{S}]/gu, '');
    return /^(嗯{1,4}|嗯哼|唔|好{1,3}(的|啊|呀)?|对{1,3}(的|啊)?|是的|明白了?|知道了|了解了?|收到|没错|ok|okay|right|uhhuh)$/.test(value);
  }

  handleRecognition(data) {
    const text = data.text;
    if (!text?.trim()) return;
    const backchannel = this.isOutputActive && this.isBackchannel(text);
    // A short partial might later grow into “好，但是…”. Keep showing it, but
    // wait for its final form before deciding that it is only an acknowledgment.
    if (data.type === 'partial') {
      if (this.isPlaybackEcho(text)) return;
      if (!backchannel) this.confirmSpeech(text);
      this.emit('onPartial', text);
      return;
    }
    if (backchannel) {
      this.speechActive = false;
      this.emit('onBackchannel', text);
      return;
    }
    if (this.isPlaybackEcho(text)) return;
    this.confirmSpeech(text, true);
    this.speechActive = false;
    this.emit('onFinal', text);
  }

  confirmSpeech(text, final = false) {
    if (!text?.trim()) return;
    const correction = this.isCorrection(text);
    if (final || correction || !this.isSpeaking || this.hasLocalSpeechEnergy()) {
      this.signalSpeech('transcript', correction);
    }
  }

  isPlaybackEcho(text) {
    if (!this.isOutputActive || !text || this.isCorrection(text) || this.hasLocalSpeechEnergy()) return false;
    const normalize = value => value.toLowerCase().replace(/[\s\p{P}\p{S}]/gu, '');
    const recognized = normalize(text);
    // AEC is primary. This second guard rejects clear copies of our own output,
    // but does not reject novel words solely because the speaker is still active.
    return recognized.length >= 18 && normalize(this.currentSpeechText).includes(recognized);
  }

  signalSpeech(source, explicitCorrection = false) {
    if (this.speechActive && !(explicitCorrection && this.isSpeaking)) return;
    this.speechActive = true;
    this.lastSpeechAt = performance.now();
    const interrupted = this.isSpeaking && this.options.bargeIn !== false;
    if (interrupted) this.interrupt();
    this.emit('onSpeech', {source, interrupted});
  }

  releaseMic() {
    if (this.processor) this.processor.onaudioprocess = null;
    for (const node of [this.micSource, this.processor, this.muteGain]) {
      try { node?.disconnect(); } catch { /* Already released. */ }
    }
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = this.micSource = this.processor = this.muteGain = null;
    this.energySince = this.lastEnergyAt = 0;
    this.speechActive = false;
  }

  stopMic({discard = false} = {}) {
    this.releaseMic();
    this.micStarting = false;
    if (this.finishMicDrain) {
      if (!discard) return this.micDrainPromise;
      this.finishMicDrain();
    }
    const socket = this.micSocket;
    if (discard || !socket || socket.readyState !== WebSocket.OPEN) {
      ++this.micGeneration;
      this.micSocket = null;
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: 'cancel'}));
      socket?.close();
      this.emit('onState', {mic: false});
      return Promise.resolve();
    }
    let resolveDrain;
    this.micDrainPromise = new Promise(resolve => {resolveDrain = resolve;});
    const generation = this.micGeneration;
    const timer = setTimeout(() => finish(), this.options.micDrainMs ?? 2500);
    const finish = () => {
      clearTimeout(timer);
      if (this.micSocket === socket) this.micSocket = null;
      if (generation === this.micGeneration) ++this.micGeneration;
      if (this.finishMicDrain === finish) this.finishMicDrain = null;
      socket.close();
      resolveDrain();
    };
    this.finishMicDrain = finish;
    socket.send(JSON.stringify({type: 'stop'}));
    this.emit('onState', {mic: false});
    return this.micDrainPromise;
  }

  /** Immediate replacement; enqueue() is the ordered, one-sentence-ahead API. */
  async speak(text) {
    this.interrupt();
    return this.playSentence(text);
  }

  /** Call immediately when a clause arrives, not inside another playback .then.
   * Resolves only after this sentence's playback or cancellation. No caller
   * should treat prefetched PCM or this Web Audio estimate as proof of hearing.
   */
  enqueue(text) {
    if (!text?.trim()) return Promise.resolve({cancelled: false, firstPCM: null, playedMs: 0, scheduledMs: 0});
    const result = new Promise((resolve, reject) => {
      if (this.speechQueue.length >= 32) {reject(new Error('待朗读句子过多，请缩短本次汇报。')); return;}
      this.speechQueue.push({text, resolve, reject});
      this.prefetchNext();
      this.pumpSpeechQueue();
    });
    // A caller may await a later segment only after the current one completes.
    // Mark rejection handled immediately without changing what that caller sees.
    result.catch(() => {});
    return result;
  }

  prefetchNext() {
    if (!this.prefetchedTTS && this.speechQueue.length) {
      this.prefetchedTTS = new TTSSource(this.socketURL('/ws/tts'), this.speechQueue[0].text);
    }
  }

  async pumpSpeechQueue() {
    if (this.queueRunning) return;
    this.queueRunning = true;
    try {
      while (this.speechQueue.length) {
        const job = this.speechQueue.shift();
        try {
          const playback = this.playSentence(job.text);
          this.prefetchNext();
          job.resolve(await playback);
        } catch (error) {
          job.reject(error);
          this.interrupt();
        }
      }
    } finally {
      this.queueRunning = false;
    }
  }

  async playSentence(text) {
    const prepared = this.prefetchedTTS?.text === text ? this.prefetchedTTS : null;
    if (prepared) this.prefetchedTTS = null;
    this.interruptCurrent();
    if (!text?.trim()) return {cancelled: false, firstPCM: null, playedMs: 0, scheduledMs: 0};
    const generation = this.speechGeneration;
    if (prepared) {this.activeTTS = prepared; this.ttsSocket = prepared.socket;}
    let context;
    try {context = await this.ensureAudio();} catch (error) {prepared?.cancel(); throw error;}
    if (generation !== this.speechGeneration) {
      prepared?.cancel();
      return {cancelled: true, firstPCM: null, playedMs: 0, scheduledMs: 0};
    }
    const source = prepared || new TTSSource(this.socketURL('/ws/tts'), text);
    const requestedAt = source.startedAt;
    this.activeTTS = source;
    this.ttsSocket = source.socket;
    this.currentSpeechText = text;
    this.metrics = {firstPCM: null, firstPlayback: null, playedMs: 0, scheduledMs: 0};
    this.nextPlayTime = context.currentTime;
    this.emit('onState', {speaking: true});
    return new Promise((resolve, reject) => {
      let settled = false;
      let generationDone = false;
      let sampleRate = 24000;
      let sawAudio = false;
      const playbackRanges = [];
      const playbackProgress = () => {
        const now = context.currentTime;
        let played = 0, scheduled = 0;
        for (const range of playbackRanges) {
          const duration = range.end - range.start;
          scheduled += duration;
          // onended confirms Web Audio finished this range. For an interrupted
          // range, currentTime estimates the completed part, excluding gaps.
          // Neither is proof that a person physically heard or understood it.
          played += range.ended ? duration : Math.max(0, Math.min(now, range.end) - range.start);
        }
        return {playedMs: Math.round(played * 1000), scheduledMs: Math.round(scheduled * 1000),
          playbackMeasurement: 'web_audio_timeline_estimate'};
      };
      const timer = setTimeout(() => fail(new Error('语音合成等待超时。')), 60000);
      const finish = (result, error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (this.activeTTS === source) {this.activeTTS = null; this.ttsSocket = null;}
        if (generation === this.speechGeneration) this.currentSpeechText = '';
        if (this.finishSpeaking === cancel) this.finishSpeaking = null;
        const measured = {...this.metrics, ...playbackProgress()};
        this.metrics = measured;
        this.emit('onMetrics', measured);
        this.emit('onState', {speaking: false});
        if (error) reject(error); else resolve({...result, ...measured});
      };
      const cancel = () => finish({cancelled: true, ...this.metrics});
      const fail = error => {
        if (generation !== this.speechGeneration || settled) return;
        this.emit('onError', error);
        source.cancel();
        finish(null, error);
        this.interrupt();
      };
      const maybeFinish = () => {
        if (generationDone && this.sources.size === 0) finish({cancelled: false, ...this.metrics});
      };
      this.finishSpeaking = cancel;
      source.attach(event => {
        if (generation !== this.speechGeneration || settled) return;
        if (event.data instanceof ArrayBuffer) {
          const pcm = event.data;
          if (!pcm.byteLength) return;
          if (pcm.byteLength % 2) return fail(new Error('语音 PCM 音频帧不完整。'));
          if (!sawAudio) {
            sawAudio = true;
            this.metrics.firstPCM = Math.round((source.firstPCMAt ?? performance.now()) - requestedAt);
          }
          if (this.nextPlayTime - context.currentTime > 45) return fail(new Error('朗读缓冲过长，请缩短内容。'));
          const view = new DataView(pcm);
          const buffer = context.createBuffer(1, pcm.byteLength / 2, sampleRate);
          const samples = buffer.getChannelData(0);
          for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
          const node = context.createBufferSource();
          node.buffer = buffer;
          node.connect(context.destination);
          const playAt = Math.max(context.currentTime + 0.025, this.nextPlayTime);
          this.nextPlayTime = playAt + buffer.duration;
          const range = {start: playAt, end: this.nextPlayTime, ended: false};
          playbackRanges.push(range);
          node.playbackStart = range.start;
          node.playbackEnd = range.end;
          this.sources.add(node);
          node.onended = () => {
            range.ended = true;
            this.sources.delete(node);
            node.disconnect();
            if (generation === this.speechGeneration) maybeFinish();
          };
          if (this.metrics.firstPlayback === null) {
            // Scheduled output time; this is not a physical speaker-loopback measurement.
            this.metrics.scheduledStartAt = performance.now() + (playAt - context.currentTime) * 1000;
            this.metrics.firstPlayback = Math.round(this.metrics.scheduledStartAt - requestedAt);
            this.emit('onMetrics', {...this.metrics, playbackMeasurement: 'scheduled Web Audio output'});
          }
          node.start(playAt);
          return;
        }
        let data;
        try { data = JSON.parse(event.data); } catch { return fail(new Error('语音服务返回无效事件。')); }
        if (data.type === 'ready') sampleRate = data.sampleRate || 24000;
        else if (data.type === 'done') {
          generationDone = true;
          clearTimeout(timer);
          this.emit('onMetrics', {...this.metrics, ...data});
          maybeFinish();
        } else if (data.type === 'error') fail(new Error(data.message || '语音合成失败。'));
        else if (data.type === 'cancelled') cancel();
      });
    });
  }

  interruptCurrent() {
    ++this.speechGeneration;
    const source = this.activeTTS;
    const socket = source ? null : this.ttsSocket;
    this.activeTTS = null;
    this.ttsSocket = null;
    source?.cancel();
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: 'cancel'}));
      socket.close();
    } else if (socket) socket.close();
    for (const source of this.sources) {
      source.onended = null;
      try { source.stop(); source.disconnect(); } catch { /* Already ended. */ }
    }
    this.sources.clear();
    this.nextPlayTime = 0;
    this.currentSpeechText = '';
    const finish = this.finishSpeaking;
    this.finishSpeaking = null;
    finish?.();
    this.emit('onState', {speaking: false});
  }

  interrupt() {
    this.interruptCurrent();
    this.prefetchedTTS?.cancel();
    this.prefetchedTTS = null;
    for (const job of this.speechQueue.splice(0)) {
      job.resolve({cancelled: true, firstPCM: null, playedMs: 0, scheduledMs: 0});
    }
  }

  async close() {
    this.stopMic({discard: true});
    this.interrupt();
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.context = null;
  }
}
