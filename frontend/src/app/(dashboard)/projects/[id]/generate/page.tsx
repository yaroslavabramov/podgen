"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "use-intl";
import Link from "next/link";
import { HTTPError } from "ky";
import { api, type CostEstimate } from "@/lib/api";
import { useGenerationStore } from "@/stores/generation-store";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { GenerationProgress } from "@/components/generation-progress";
import { CostEstimateCard } from "@/components/cost-estimate-card";
import { LocaleSwitcher } from "@/components/locale-switcher";

interface VoiceOption {
  voice_id: string;
  name: string;
  category?: string;
}

interface ShowBibleSummary {
  show_style: string;
  tone_and_mood: string;
  hosts?: Array<{ name?: string }>;
}

interface GenerationRunStart {
  id: string;
}

export default function GeneratePage() {
  const t = useTranslations("generatePage");
  const params = useParams();
  const projectId = params.id as string;

  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(1800);
  const [cost, setCost] = useState<CostEstimate | null>(null);
  const [voices, setVoices] = useState<VoiceOption[]>([]);
  const [voiceA, setVoiceA] = useState("");
  const [voiceB, setVoiceB] = useState("");
  const [showBibleSummary, setShowBibleSummary] = useState<ShowBibleSummary | null>(null);
  const [launching, setLaunching] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const { runId, status, startListening, reset } = useGenerationStore();

  const extractApiError = async (error: unknown, fallback: string) => {
    if (error instanceof HTTPError) {
      try {
        const payload = await error.response.clone().json() as { detail?: string };
        if (payload?.detail) return payload.detail;
      } catch {
        // ignore parse failures
      }
      return error.message || fallback;
    }
    if (error instanceof Error) return error.message || fallback;
    return fallback;
  };

  useEffect(() => {
    if (!prompt) return;
    const timer = setTimeout(async () => {
      try {
        const estimate = await api.post(`api/projects/${projectId}/estimate`, {
          json: { duration_target: duration },
        }).json<CostEstimate>();
        setCost(estimate);
      } catch {
        // ignore
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [duration, prompt, projectId]);

  useEffect(() => {
    let cancelled = false;

    async function loadGenerationConfig() {
      const [voicesResult, bibleResult] = await Promise.allSettled([
        api.get("api/voices").json<VoiceOption[]>(),
        api.get(`api/projects/${projectId}/show-bible`).json<ShowBibleSummary>(),
      ]);

      if (cancelled) return;

      if (voicesResult.status === "fulfilled") {
        const loadedVoices = voicesResult.value;
        setVoices(loadedVoices);
        if (loadedVoices.length > 0) {
          setVoiceA((prev) => prev || loadedVoices[0].voice_id);
          setVoiceB((prev) => prev || loadedVoices[1]?.voice_id || loadedVoices[0].voice_id);
        }
      }

      if (bibleResult.status === "fulfilled") {
        setShowBibleSummary(bibleResult.value);
      } else {
        setShowBibleSummary(null);
      }
    }

    loadGenerationConfig().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const handleGenerate = async () => {
    setLaunching(true);
    setActionError(null);
    try {
      const run = await api.post(`api/projects/${projectId}/generate`, {
        json: {
          duration_target: duration,
          user_prompt: prompt,
          voice_a_id: voiceA || null,
          voice_b_id: voiceB || null,
        },
      }).json<GenerationRunStart>();
      startListening(run.id);
    } catch (err) {
      console.error("Failed to start generation:", err);
      setActionError(await extractApiError(err, t("startFailed")));
    } finally {
      setLaunching(false);
    }
  };

  const handleResume = async () => {
    if (!runId) return;
    setResuming(true);
    setActionError(null);
    try {
      const run = await api.post(`api/generations/${runId}/resume`).json<GenerationRunStart>();
      startListening(run.id);
    } catch (err) {
      setActionError(await extractApiError(err, t("resumeFailed")));
    } finally {
      setResuming(false);
    }
  };

  if (runId && status !== "idle") {
    return (
      <div className="min-h-screen bg-background">
        <header className="border-b border-border px-6 py-4 flex items-center justify-between">
          <h1 className="text-xl font-bold">{t("runningTitle")}</h1>
          <LocaleSwitcher />
        </header>
        <main className="max-w-4xl mx-auto px-6 py-8">
          <GenerationProgress />
          {(status === "failed" || status === "completed") && (
            <Card className="mt-6">
              <CardContent className="py-4 space-y-3">
                <div className="flex flex-wrap gap-3">
                  {status === "failed" && (
                    <Button type="button" onClick={handleResume} disabled={resuming}>
                      {resuming ? t("retrying") : t("retryFailedRun")}
                    </Button>
                  )}
                  <Button type="button" variant="outline" onClick={reset}>
                    {t("startNewRun")}
                  </Button>
                </div>
                {actionError && <p className="text-sm text-destructive">{actionError}</p>}
              </CardContent>
            </Card>
          )}
        </main>
      </div>
    );
  }

  const durationLabels: Record<number, string> = {
    900: "15 min", 1800: "30 min", 2700: "45 min", 3600: "60 min", 5400: "90 min",
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">{t("title")}</h1>
        <LocaleSwitcher />
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>{t("topicPrompt")}</CardTitle>
          </CardHeader>
          <CardContent>
            <Textarea
              placeholder={t("topicPlaceholder")}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={4}
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("duration")}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-3">
              {[900, 1800, 2700, 3600, 5400].map((d) => (
                <button
                  key={d}
                  onClick={() => setDuration(d)}
                  className={`px-4 py-2 rounded-md text-sm font-medium border transition-colors ${
                    duration === d
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:border-primary/50"
                  }`}
                >
                  {durationLabels[d]}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("voices")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {voices.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("voicesLoading")}</p>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <label className="text-sm font-medium">{t("voiceA")}</label>
                  <select
                    value={voiceA}
                    onChange={(e) => setVoiceA(e.target.value)}
                    className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background"
                  >
                    {voices.map((voice) => (
                      <option key={voice.voice_id} value={voice.voice_id}>
                        {voice.name}{voice.category ? ` (${voice.category})` : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <label className="text-sm font-medium">{t("voiceB")}</label>
                  <select
                    value={voiceB}
                    onChange={(e) => setVoiceB(e.target.value)}
                    className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background"
                  >
                    {voices.map((voice) => (
                      <option key={voice.voice_id} value={voice.voice_id}>
                        {voice.name}{voice.category ? ` (${voice.category})` : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("showBibleSummary")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {showBibleSummary ? (
              <>
                <p className="text-sm text-muted-foreground">
                  {t("showStyle")}: <span className="text-foreground">{showBibleSummary.show_style}</span>
                </p>
                <p className="text-sm text-muted-foreground">
                  {t("tone")}: <span className="text-foreground">{showBibleSummary.tone_and_mood}</span>
                </p>
                {showBibleSummary.hosts && showBibleSummary.hosts.length > 0 && (
                  <p className="text-sm text-muted-foreground">
                    {t("hosts")}:{" "}
                    <span className="text-foreground">
                      {showBibleSummary.hosts.map((h) => h.name).filter(Boolean).join(", ")}
                    </span>
                  </p>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">{t("noShowBible")}</p>
            )}
            <Link href={`/projects/${projectId}`} className="inline-block">
              <Button variant="ghost" size="sm">{t("editShowBible")}</Button>
            </Link>
          </CardContent>
        </Card>

        {cost && <CostEstimateCard cost={cost} />}

        <Button
          type="button"
          size="lg"
          className="w-full"
          onClick={handleGenerate}
          disabled={!prompt.trim() || launching}
        >
          {launching ? t("starting") : t("startGeneration")}
        </Button>
        {actionError && <p className="text-sm text-destructive">{actionError}</p>}
      </main>
    </div>
  );
}
