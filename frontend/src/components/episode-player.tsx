"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type WaveSurfer from "wavesurfer.js";

interface TranscriptEntry {
  start_ms: number;
  end_ms: number;
  speaker: string;
  text: string;
}

interface Props {
  episodeId: string;
  transcript: TranscriptEntry[];
  durationSeconds: number;
}

function buildTranscriptPeaks(transcript: TranscriptEntry[], durationMs: number, bins = 320): number[] {
  const safeDuration = Math.max(durationMs, 1);
  const peaks = Array.from({ length: bins }, () => 0.05);

  if (transcript.length === 0) return peaks;

  for (const line of transcript) {
    const start = Math.max(0, Math.min(safeDuration, line.start_ms));
    const end = Math.max(start + 1, Math.min(safeDuration, line.end_ms));
    const textWeight = Math.min(1, 0.2 + (line.text?.length || 0) / 240);
    const startBin = Math.floor((start / safeDuration) * bins);
    const endBin = Math.max(startBin, Math.floor((end / safeDuration) * bins));
    for (let i = startBin; i <= endBin && i < bins; i += 1) {
      peaks[i] = Math.max(peaks[i], textWeight);
    }
  }

  const maxPeak = Math.max(...peaks);
  if (maxPeak <= 0) return peaks;
  return peaks.map((p) => Math.max(0.03, Math.min(1, p / maxPeak)));
}

export function EpisodePlayer({ episodeId, transcript, durationSeconds }: Props) {
  const t = useTranslations("episodePlayer");
  const waveformRef = useRef<HTMLDivElement>(null);
  const wavesurferRef = useRef<WaveSurfer | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loadingAudio, setLoadingAudio] = useState(true);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null);

  const apiBase = process.env.NEXT_PUBLIC_API_URL || "";
  const playbackUrlApi = `${apiBase}/api/episodes/${episodeId}/audio-url`;
  const transcriptMaxEndMs = transcript.reduce((max, row) => Math.max(max, row.end_ms), 0);
  const estimatedDurationMs = Math.max(durationSeconds * 1000, transcriptMaxEndMs, 1);

  useEffect(() => {
    let cancelled = false;
    setLoadingAudio(true);
    setAudioError(null);
    setPlaybackUrl(null);
    setDownloadUrl(null);

    async function fetchPlaybackUrl() {
      try {
        const token = localStorage.getItem("token");
        if (!token) throw new Error(t("missingAuth"));

        const response = await fetch(playbackUrlApi, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error(t("failedLoad"));

        const data = await response.json() as { url?: string };
        if (!data.url) throw new Error(t("failedLoad"));
        if (cancelled) return;

        const absoluteUrl = data.url.startsWith("http")
          ? data.url
          : `${apiBase}${data.url}`;
        setPlaybackUrl(absoluteUrl);
        setDownloadUrl(absoluteUrl);
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : t("failedInit");
          setAudioError(message);
          setLoadingAudio(false);
        }
      }
    }

    fetchPlaybackUrl();

    return () => {
      cancelled = true;
    };
  }, [apiBase, playbackUrlApi, t]);

  useEffect(() => {
    if (!playbackUrl) return;
    const streamUrl = playbackUrl;

    let cancelled = false;
    setCurrentTime(0);
    setDuration(0);
    setPlaying(false);

    async function setupWaveform() {
      const container = waveformRef.current;
      if (!container) return;
      container.innerHTML = "";

      try {
        const module = await import("wavesurfer.js");
        if (cancelled || !waveformRef.current) return;

        const rootStyles = getComputedStyle(document.documentElement);
        const waveColor = rootStyles.getPropertyValue("--color-muted-foreground").trim() || "#9ca3af";
        const progressColor = rootStyles.getPropertyValue("--color-primary").trim() || "#6d28d9";
        const peaks = buildTranscriptPeaks(transcript, estimatedDurationMs);

        const ws = module.default.create({
          container: waveformRef.current,
          url: streamUrl,
          waveColor,
          progressColor,
          cursorColor: progressColor,
          height: 80,
          normalize: true,
          barWidth: 2,
          barGap: 2,
          barRadius: 4,
          dragToSeek: true,
          backend: "MediaElement",
          peaks: [peaks],
          duration: estimatedDurationMs / 1000,
        });

        wavesurferRef.current = ws;
        ws.on("ready", () => {
          setDuration(ws.getDuration() * 1000);
          setLoadingAudio(false);
        });
        ws.on("timeupdate", (seconds: number) => {
          setCurrentTime(seconds * 1000);
        });
        ws.on("play", () => setPlaying(true));
        ws.on("pause", () => setPlaying(false));
        ws.on("finish", () => {
          setPlaying(false);
          setCurrentTime(ws.getDuration() * 1000);
        });
        ws.on("error", (err: Error) => {
          setAudioError(err.message || t("failedInit"));
          setLoadingAudio(false);
        });
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : t("failedInit");
          setAudioError(message);
          setLoadingAudio(false);
        }
      }
    }

    setupWaveform();

    return () => {
      cancelled = true;
      const ws = wavesurferRef.current;
      if (ws) {
        ws.destroy();
        wavesurferRef.current = null;
      }
    };
  }, [estimatedDurationMs, playbackUrl, t, transcript]);

  const togglePlay = () => {
    const ws = wavesurferRef.current;
    if (!ws || loadingAudio || audioError) return;
    ws.playPause();
    setPlaying(ws.isPlaying());
  };

  const seekTo = (ms: number) => {
    const ws = wavesurferRef.current;
    if (!ws) return;
    ws.setTime(ms / 1000);
    setCurrentTime(ms);
  };

  const formatTime = (ms: number) => {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };

  // Find current transcript entry
  const currentEntry = transcript.find(
    (e) => currentTime >= e.start_ms && currentTime < e.end_ms
  );

  return (
    <Card>
      <CardContent className="py-4 space-y-4">
        <div className="space-y-2">
          <div ref={waveformRef} className="w-full" />
          {loadingAudio && <p className="text-xs text-muted-foreground">{t("loadingWaveform")}</p>}
          {audioError && <p className="text-xs text-destructive">{audioError}</p>}
        </div>

        <div className="flex items-center gap-4">
          <Button onClick={togglePlay} variant="outline" size="icon" disabled={loadingAudio || !!audioError}>
            {playing ? "\u23F8" : "\u25B6"}
          </Button>
          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {formatTime(currentTime)} / {formatTime(duration)}
          </span>
          {downloadUrl && !audioError && (
            <a href={downloadUrl} download={`episode-${episodeId}.mp3`}>
              <Button variant="ghost" size="sm">{t("download")}</Button>
            </a>
          )}
        </div>

        {/* Synced transcript */}
        <div className="max-h-48 overflow-y-auto space-y-1">
          {transcript.map((entry, i) => {
            const isActive = entry === currentEntry;
            return (
              <button
                key={i}
                onClick={() => seekTo(entry.start_ms)}
                className={`flex gap-2 w-full text-left p-1 rounded text-sm transition-colors ${
                  isActive ? "bg-primary/10" : "hover:bg-muted"
                }`}
              >
                <span className={`font-mono text-xs px-1 rounded ${
                  entry.speaker === "A" ? "text-primary" : "text-accent"
                }`}>
                  {entry.speaker}
                </span>
                <span className={isActive ? "font-medium" : "text-muted-foreground"}>
                  {entry.text}
                </span>
              </button>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
