"use client";

import { useGenerationStore } from "@/stores/generation-store";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { useTranslations } from "use-intl";

function pickEventText(event: Record<string, unknown>, t: (key: string, values?: Record<string, string>) => string) {
  const asString = (value: unknown) => (typeof value === "string" ? value : "");

  const direct =
    asString(event.message) ||
    asString(event.reason) ||
    asString(event.description) ||
    asString(event.detail) ||
    asString(event.statement_preview);

  if (direct) return direct;

  const sourceTitle = asString(event.source_title);
  if (sourceTitle) return t("ingested", { title: sourceTitle });

  const phase = asString(event.phase);
  const step = asString(event.step);
  return `${phase} ${step}`.trim() || t("fallbackEvent");
}

export function GenerationProgress() {
  const t = useTranslations("generationProgress");
  const { status, currentPhase, progressPct, events, episodeId, error } = useGenerationStore();
  const v1Phases = [
    { id: "phase0_setup", label: t("phaseSetup"), icon: "1" },
    { id: "phase2_decomposition", label: t("phaseDecompose"), icon: "2" },
    { id: "phase3_research", label: t("phaseResearch"), icon: "3" },
    { id: "phase4_claims", label: t("phaseClaims"), icon: "4" },
    { id: "phase5_planning", label: t("phasePlanning"), icon: "5" },
    { id: "phase6_writing", label: t("phaseWriting"), icon: "6" },
    { id: "phase7_audio", label: t("phaseAudio"), icon: "7" },
  ];
  const v2Phases = [
    { id: "v2_corpus_prepare", label: "Corpus", icon: "1" },
    { id: "v2_source_triage", label: "Source Triage", icon: "2" },
    { id: "v2_source_acquisition", label: "Acquisition", icon: "3" },
    { id: "v2_editorial_research", label: "Editorial Research", icon: "4" },
    { id: "v2_research_audit", label: "Research Audit", icon: "5" },
    { id: "v2_treatment", label: "Treatment", icon: "6" },
    { id: "v2_act_drafting", label: "Act Drafts", icon: "7" },
    { id: "v2_critique_rewrite", label: "Critique", icon: "8" },
    { id: "v2_fact_citation", label: "Citations", icon: "9" },
    { id: "v2_audio", label: t("phaseAudio"), icon: "10" },
  ];
  const phases = currentPhase.startsWith("v2_") ? v2Phases : v1Phases;

  const currentPhaseIndex = phases.findIndex((p) => p.id === currentPhase);

  return (
    <div className="space-y-6">
      {/* Phase stepper */}
      <div className="flex items-center gap-1">
        {phases.map((phase, i) => {
          const isActive = phase.id === currentPhase;
          const isCompleted = i < currentPhaseIndex;
          return (
            <div key={phase.id} className="flex items-center flex-1">
              <div className={`flex items-center justify-center w-8 h-8 rounded-full text-xs font-bold ${
                isCompleted ? "bg-green-500 text-white" :
                isActive ? "bg-primary text-primary-foreground animate-pulse" :
                "bg-muted text-muted-foreground"
              }`}>
                {isCompleted ? "\u2713" : phase.icon}
              </div>
              <span className={`ml-1 text-xs hidden lg:inline ${isActive ? "text-primary font-medium" : "text-muted-foreground"}`}>
                {phase.label}
              </span>
              {i < phases.length - 1 && (
                <div className={`flex-1 h-0.5 mx-2 ${isCompleted ? "bg-green-500" : "bg-muted"}`} />
              )}
            </div>
          );
        })}
      </div>

      {/* Progress bar */}
      <div className="space-y-2">
        <div className="flex justify-between text-sm">
          <span className="text-muted-foreground">{t("progress")}</span>
          <span className="font-medium">{Math.round(progressPct)}%</span>
        </div>
        <Progress value={progressPct} />
      </div>

      {/* Status */}
      {status === "completed" && episodeId && (
        <Card className="border-green-500/50 bg-green-500/5">
          <CardContent className="py-6 text-center">
            <p className="text-lg font-semibold text-green-600 mb-3">{t("completed")}</p>
            <Link href={`/episodes/${episodeId}`}>
              <Button>{t("listen")}</Button>
            </Link>
          </CardContent>
        </Card>
      )}

      {status === "failed" && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-6 text-center">
            <p className="text-lg font-semibold text-destructive">{t("failed")}</p>
            <p className="text-sm text-muted-foreground mt-1">{error}</p>
          </CardContent>
        </Card>
      )}

      {status === "cancelled" && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="py-6 text-center">
            <p className="text-lg font-semibold text-amber-600">{t("cancelled")}</p>
          </CardContent>
        </Card>
      )}

      {/* Live event feed */}
      <Card>
        <CardContent className="py-4">
          <h3 className="text-sm font-medium mb-3">{t("liveFeed")}</h3>
          <div className="space-y-1 max-h-64 overflow-y-auto text-sm">
            {events.slice(-20).map((event, i) => (
              <div key={i} className="flex items-center gap-2 py-0.5">
                <Badge variant="secondary" className="text-[10px] shrink-0">
                  {event.type.replace(/_/g, " ")}
                </Badge>
                <span className="text-muted-foreground truncate">
                  {pickEventText(event, (key, values) => t(key, values))}
                </span>
              </div>
            ))}
            {events.length === 0 && (
              <p className="text-muted-foreground">{t("waiting")}</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
