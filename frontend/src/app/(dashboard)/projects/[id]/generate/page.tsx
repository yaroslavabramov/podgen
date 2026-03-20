"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "use-intl";
import Link from "next/link";
import { HTTPError } from "ky";
import { api, type CostEstimate, type GenerationRun } from "@/lib/api";
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

type ExecutionMode = "full" | "step";

interface StepControlState {
  last_node: string | null;
  next_node: string | null;
  available_rerun_nodes: string[];
  is_complete: boolean;
  can_rerun: boolean;
  requested_action: string | null;
  requested_target_node: string | null;
  stop_requested: boolean;
  stop_reason: string | null;
}

interface RunStopState {
  stop_requested: boolean;
  stop_reason: string | null;
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
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("full");
  const [activeExecutionMode, setActiveExecutionMode] = useState<ExecutionMode | null>(null);
  const [stepControl, setStepControl] = useState<StepControlState | null>(null);
  const [runStopState, setRunStopState] = useState<RunStopState>({ stop_requested: false, stop_reason: null });
  const [stepActionLoading, setStepActionLoading] = useState<"next" | "rerun" | "stop" | null>(null);
  const [fullStopLoading, setFullStopLoading] = useState(false);
  const [selectedRerunNode, setSelectedRerunNode] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [autoRestoreSuppressed, setAutoRestoreSuppressed] = useState(false);

  const { runId, status, events, startListening, applyRunSnapshot, reset } = useGenerationStore();
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;

  const parseStepControl = (config: unknown): StepControlState | null => {
    if (!config || typeof config !== "object") return null;
    const stepControlRaw = (config as { step_control?: unknown }).step_control;
    if (!stepControlRaw || typeof stepControlRaw !== "object") return null;
    const stepControlData = stepControlRaw as Record<string, unknown>;
    return {
      last_node: typeof stepControlData.last_node === "string" ? stepControlData.last_node : null,
      next_node: typeof stepControlData.next_node === "string" ? stepControlData.next_node : null,
      available_rerun_nodes: Array.isArray(stepControlData.available_rerun_nodes)
        ? stepControlData.available_rerun_nodes.filter((value): value is string => typeof value === "string" && value.length > 0)
        : [],
      is_complete: Boolean(stepControlData.is_complete),
      can_rerun: Boolean(stepControlData.can_rerun),
      requested_action: typeof stepControlData.requested_action === "string" ? stepControlData.requested_action : null,
      requested_target_node: typeof stepControlData.requested_target_node === "string" ? stepControlData.requested_target_node : null,
      stop_requested: Boolean(stepControlData.stop_requested),
      stop_reason: typeof stepControlData.stop_reason === "string" ? stepControlData.stop_reason : null,
    };
  };

  const parseExecutionMode = (config: unknown): ExecutionMode => {
    if (!config || typeof config !== "object") return "full";
    return (config as { execution_mode?: string }).execution_mode === "step" ? "step" : "full";
  };

  const parseRunStopState = (config: unknown): RunStopState => {
    if (!config || typeof config !== "object") {
      return { stop_requested: false, stop_reason: null };
    }
    const configData = config as Record<string, unknown>;
    return {
      stop_requested: Boolean(configData.stop_requested),
      stop_reason: typeof configData.stop_reason === "string" ? configData.stop_reason : null,
    };
  };

  const applyRunData = (run: GenerationRun) => {
    applyRunSnapshot({
      status: run.status,
      current_phase: run.current_phase,
      current_step: run.current_step,
      progress_pct: run.progress_pct,
    });
    const mode = parseExecutionMode(run.config);
    setActiveExecutionMode(mode);
    setRunStopState(mode === "full" ? parseRunStopState(run.config) : { stop_requested: false, stop_reason: null });
    setStepControl(mode === "step" ? parseStepControl(run.config) : null);
  };

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

  const nodeLabelMap: Record<string, string> = {
    setup_show: t("stepNode.setup_show"),
    decompose: t("stepNode.decompose"),
    hunt_closed_book: t("stepNode.hunt_closed_book"),
    research_fast_web: t("stepNode.research_fast_web"),
    build_claims: t("stepNode.build_claims"),
    dedupe_claims: t("stepNode.dedupe_claims"),
    verify_claims: t("stepNode.verify_claims"),
    resolve_conflicts: t("stepNode.resolve_conflicts"),
    plan_episode: t("stepNode.plan_episode"),
    plan_acts: t("stepNode.plan_acts"),
    integrate_acts: t("stepNode.integrate_acts"),
    write_screenplay: t("stepNode.write_screenplay"),
    evaluate_and_revolt: t("stepNode.evaluate_and_revolt"),
    audit_citations: t("stepNode.audit_citations"),
    generate_audio: t("stepNode.generate_audio"),
    build_shownotes: t("stepNode.build_shownotes"),
  };

  const formatNodeLabel = (node: string | null) => {
    if (!node) return t("stepUnknown");
    return nodeLabelMap[node] || node;
  };

  const stepSequence = [
    "setup_show",
    "decompose",
    "hunt_closed_book",
    "research_fast_web",
    "build_claims",
    "dedupe_claims",
    "verify_claims",
    "resolve_conflicts",
    "plan_episode",
    "plan_acts",
    "integrate_acts",
    "write_screenplay",
    "evaluate_and_revolt",
    "audit_citations",
    "generate_audio",
    "build_shownotes",
  ];

  const refreshRun = async (currentRunId: string) => {
    try {
      const run = await api.get(`api/generations/${currentRunId}`).json<GenerationRun>();
      applyRunData(run);
    } catch {
      // keep previous UI state on refresh failure
    }
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

  useEffect(() => {
    if (runId || status !== "idle" || autoRestoreSuppressed) return;

    let cancelled = false;

    async function restoreLatestRun() {
      try {
        const run = await api.get(`api/projects/${projectId}/generations/latest`).json<GenerationRun>();
        if (cancelled) return;
        startListening(run.id);
        applyRunData(run);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof HTTPError && error.response.status === 404) {
          return;
        }
      }
    }

    restoreLatestRun().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [projectId, runId, status, startListening, autoRestoreSuppressed]);

  useEffect(() => {
    if (!runId || activeExecutionMode !== "step" || !lastEvent) return;
    if (!["generation_paused", "generation_failed", "generation_completed"].includes(lastEvent.type)) return;
    refreshRun(runId).catch(() => undefined);
  }, [runId, activeExecutionMode, lastEvent]);

  useEffect(() => {
    if (!runId || activeExecutionMode !== "step") return;

    const shouldPoll =
      stepActionLoading !== null ||
      status === "running" ||
      Boolean(stepControl?.requested_action) ||
      Boolean(stepControl?.stop_requested);

    if (!shouldPoll) return;

    const intervalId = window.setInterval(() => {
      refreshRun(runId).catch(() => undefined);
    }, 1500);

    return () => window.clearInterval(intervalId);
  }, [runId, activeExecutionMode, stepActionLoading, status, stepControl]);

  const derivedRerunNodes = (() => {
    if (!stepControl?.last_node) return [];
    const lastIndex = stepSequence.indexOf(stepControl.last_node);
    if (lastIndex === -1) return [stepControl.last_node];
    return stepSequence.slice(0, lastIndex + 1);
  })();

  const availableRerunNodes =
    stepControl?.available_rerun_nodes && stepControl.available_rerun_nodes.length > 0
      ? stepControl.available_rerun_nodes
      : derivedRerunNodes;

  useEffect(() => {
    if (activeExecutionMode !== "step") {
      setSelectedRerunNode(null);
      return;
    }
    const activeRequestedNode =
      stepControl?.requested_action === "rerun" ? stepControl?.requested_target_node || null : null;
    if (activeRequestedNode && availableRerunNodes.includes(activeRequestedNode)) {
      setSelectedRerunNode(activeRequestedNode);
      return;
    }
    if (selectedRerunNode && availableRerunNodes.includes(selectedRerunNode)) return;
    const fallbackNode =
      stepControl?.last_node && availableRerunNodes.includes(stepControl.last_node)
        ? stepControl.last_node
        : availableRerunNodes.length > 0
          ? availableRerunNodes[availableRerunNodes.length - 1]
          : null;
    setSelectedRerunNode(fallbackNode);
  }, [activeExecutionMode, stepControl, availableRerunNodes, selectedRerunNode]);

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
          execution_mode: executionMode,
        },
      }).json<GenerationRun>();
      setAutoRestoreSuppressed(false);
      startListening(run.id);
      if (executionMode === "step") {
        applyRunData(run);
      } else {
        setActiveExecutionMode("full");
        setStepControl(null);
        setRunStopState({ stop_requested: false, stop_reason: null });
      }
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
    setAutoRestoreSuppressed(false);
    try {
      const run = await api.post(`api/generations/${runId}/resume`).json<GenerationRun>();
      startListening(run.id);
      setActiveExecutionMode("full");
      setStepControl(null);
      setRunStopState({ stop_requested: false, stop_reason: null });
    } catch (err) {
      setActionError(await extractApiError(err, t("resumeFailed")));
    } finally {
      setResuming(false);
    }
  };

  const handleRunNextStep = async () => {
    if (!runId) return;
    setStepActionLoading("next");
    setActionError(null);
    try {
      const run = await api.post(`api/generations/${runId}/steps/next`).json<GenerationRun>();
      applyRunData(run);
    } catch (err) {
      setActionError(await extractApiError(err, t("stepNextFailed")));
    } finally {
      setStepActionLoading(null);
    }
  };

  const handleRerunStep = async () => {
    if (!runId) return;
    setStepActionLoading("rerun");
    setActionError(null);
    try {
      const targetNode = selectedRerunNode ?? stepControl?.last_node ?? null;
      const run = await api.post(`api/generations/${runId}/steps/rerun`, {
        json: { target_node: targetNode },
      }).json<GenerationRun>();
      applyRunData(run);
    } catch (err) {
      setActionError(await extractApiError(err, t("stepRerunFailed")));
    } finally {
      setStepActionLoading(null);
    }
  };

  const handleStopStep = async () => {
    if (!runId) return;
    setStepActionLoading("stop");
    setActionError(null);
    try {
      const run = await api.post(`api/generations/${runId}/steps/stop`).json<GenerationRun>();
      applyRunData(run);
    } catch (err) {
      setActionError(await extractApiError(err, t("stepStopFailed")));
    } finally {
      setStepActionLoading(null);
    }
  };

  const handleStopGeneration = async () => {
    if (!runId) return;
    setFullStopLoading(true);
    setActionError(null);
    try {
      const run = await api.post(`api/generations/${runId}/cancel`).json<GenerationRun>();
      applyRunData(run);
    } catch (err) {
      setActionError(await extractApiError(err, t("stopFailed")));
    } finally {
      setFullStopLoading(false);
    }
  };

  const handleResetRun = () => {
    setAutoRestoreSuppressed(true);
    reset();
    setActiveExecutionMode(null);
    setStepControl(null);
    setRunStopState({ stop_requested: false, stop_reason: null });
    setActionError(null);
  };

  const isStepModeRun = activeExecutionMode === "step";
  const hasQueuedStepAction = Boolean(stepControl?.requested_action);
  const stopRequested = Boolean(stepControl?.stop_requested);
  const stepBusy = stepActionLoading !== null || status === "running" || hasQueuedStepAction || stopRequested;
  const canRunNextStep = isStepModeRun && !stepBusy && !stepControl?.is_complete;
  const canRerunStep = isStepModeRun && !stepBusy && Boolean(stepControl?.can_rerun && selectedRerunNode);
  const canStopStep = isStepModeRun && !stepActionLoading && (status === "running" || hasQueuedStepAction) && !stopRequested;
  const isFullModeRun = activeExecutionMode === "full";
  const fullStopRequested = Boolean(runStopState.stop_requested);
  const canStopGeneration =
    isFullModeRun &&
    !fullStopLoading &&
    !fullStopRequested &&
    (status === "running" || status === "connecting");

  if (runId && status !== "idle") {
    return (
      <div className="min-h-screen bg-background">
        <header className="border-b border-border px-6 py-4 flex items-center justify-between">
          <h1 className="text-xl font-bold">{t("runningTitle")}</h1>
          <LocaleSwitcher />
        </header>
        <main className="max-w-4xl mx-auto px-6 py-8">
          {isStepModeRun && (
            <Card className="mb-6">
              <CardHeader>
                <CardTitle>{t("stepModeTitle")}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="grid gap-2 text-sm text-muted-foreground sm:grid-cols-2">
                  <p>
                    {t("stepLastNode")}:{" "}
                    <span className="text-foreground">{formatNodeLabel(stepControl?.last_node ?? null)}</span>
                  </p>
                  <p>
                    {t("stepNextNode")}:{" "}
                    <span className="text-foreground">{formatNodeLabel(stepControl?.next_node ?? null)}</span>
                  </p>
                </div>
                <div className="space-y-1">
                  <label className="text-sm text-muted-foreground" htmlFor="rerun-step-select">
                    {t("stepRerunTarget")}
                  </label>
                  <select
                    id="rerun-step-select"
                    className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
                    value={selectedRerunNode ?? ""}
                    onChange={(event) => setSelectedRerunNode(event.target.value || null)}
                    disabled={stepBusy || availableRerunNodes.length === 0}
                  >
                    {availableRerunNodes.map((node) => (
                      <option key={node} value={node}>
                        {formatNodeLabel(node)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex flex-wrap gap-3">
                  <Button
                    type="button"
                    onClick={handleRunNextStep}
                    disabled={!canRunNextStep}
                  >
                    {stepActionLoading === "next" ? t("stepRunningNext") : t("stepRunNext")}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleRerunStep}
                    disabled={!canRerunStep}
                  >
                    {stepActionLoading === "rerun" ? t("stepRerunning") : t("stepRerun")}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleStopStep}
                    disabled={!canStopStep}
                  >
                    {stepActionLoading === "stop" || stopRequested ? t("stepStopping") : t("stepStop")}
                  </Button>
                </div>
                {stopRequested && (
                  <p className="text-sm text-muted-foreground">
                    {t("stepStopRequested")}
                  </p>
                )}
              </CardContent>
            </Card>
          )}

          {isFullModeRun && (status === "running" || status === "connecting") && (
            <Card className="mb-6">
              <CardHeader>
                <CardTitle>{t("runControlsTitle")}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex flex-wrap gap-3">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleStopGeneration}
                    disabled={!canStopGeneration}
                  >
                    {fullStopLoading || fullStopRequested ? t("stopStopping") : t("stopGeneration")}
                  </Button>
                </div>
                {fullStopRequested && (
                  <p className="text-sm text-muted-foreground">
                    {t("stopRequested")}
                  </p>
                )}
              </CardContent>
            </Card>
          )}

          <GenerationProgress />
          {(status === "failed" || status === "completed" || status === "cancelled" || isStepModeRun) && (
            <Card className="mt-6">
              <CardContent className="py-4 space-y-3">
                <div className="flex flex-wrap gap-3">
                  {status === "failed" && (
                    <Button
                      type="button"
                      onClick={isStepModeRun ? handleRunNextStep : handleResume}
                      disabled={isStepModeRun ? !canRunNextStep : resuming}
                    >
                      {resuming ? t("retrying") : t("retryFailedRun")}
                    </Button>
                  )}
                  <Button type="button" variant="outline" onClick={handleResetRun}>
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

        <Card>
          <CardHeader>
            <CardTitle>{t("executionModeTitle")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <label className="flex items-start gap-3">
              <input
                type="checkbox"
                checked={executionMode === "step"}
                onChange={(e) => setExecutionMode(e.target.checked ? "step" : "full")}
                className="mt-1"
              />
              <span className="text-sm text-muted-foreground">
                <span className="text-foreground font-medium">{t("executionModeStepLabel")}</span>{" "}
                {t("executionModeStepHint")}
              </span>
            </label>
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
