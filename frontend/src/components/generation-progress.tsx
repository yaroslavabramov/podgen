"use client";

import { useGenerationStore } from "@/stores/generation-store";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import Link from "next/link";
import { Button } from "@/components/ui/button";

const PHASES = [
  { id: "phase0_setup", label: "Show Setup", icon: "1" },
  { id: "phase2_decomposition", label: "Research Planning", icon: "2" },
  { id: "phase3_research", label: "Research", icon: "3" },
  { id: "phase4_claims", label: "Fact-Checking", icon: "4" },
  { id: "phase5_planning", label: "Episode Planning", icon: "5" },
  { id: "phase6_writing", label: "Writing", icon: "6" },
  { id: "phase7_audio", label: "Audio Generation", icon: "7" },
];

export function GenerationProgress() {
  const { status, currentPhase, progressPct, events, episodeId, error } = useGenerationStore();

  const currentPhaseIndex = PHASES.findIndex((p) => p.id === currentPhase);

  return (
    <div className="space-y-6">
      {/* Phase stepper */}
      <div className="flex items-center gap-1">
        {PHASES.map((phase, i) => {
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
              {i < PHASES.length - 1 && (
                <div className={`flex-1 h-0.5 mx-2 ${isCompleted ? "bg-green-500" : "bg-muted"}`} />
              )}
            </div>
          );
        })}
      </div>

      {/* Progress bar */}
      <div className="space-y-2">
        <div className="flex justify-between text-sm">
          <span className="text-muted-foreground">Progress</span>
          <span className="font-medium">{Math.round(progressPct)}%</span>
        </div>
        <Progress value={progressPct} />
      </div>

      {/* Status */}
      {status === "completed" && episodeId && (
        <Card className="border-green-500/50 bg-green-500/5">
          <CardContent className="py-6 text-center">
            <p className="text-lg font-semibold text-green-600 mb-3">Episode Generated!</p>
            <Link href={`/episodes/${episodeId}`}>
              <Button>Listen to Episode</Button>
            </Link>
          </CardContent>
        </Card>
      )}

      {status === "failed" && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-6 text-center">
            <p className="text-lg font-semibold text-destructive">Generation Failed</p>
            <p className="text-sm text-muted-foreground mt-1">{error}</p>
          </CardContent>
        </Card>
      )}

      {/* Live event feed */}
      <Card>
        <CardContent className="py-4">
          <h3 className="text-sm font-medium mb-3">Live Feed</h3>
          <div className="space-y-1 max-h-64 overflow-y-auto text-sm">
            {events.slice(-20).map((event, i) => (
              <div key={i} className="flex items-center gap-2 py-0.5">
                <Badge variant="secondary" className="text-[10px] shrink-0">
                  {event.type.replace(/_/g, " ")}
                </Badge>
                <span className="text-muted-foreground truncate">
                  {event.message || event.description || event.detail ||
                   (event.statement_preview) ||
                   (event.source_title ? `Ingested: ${event.source_title}` : "") ||
                   `${event.phase || ""} ${event.step || ""}`}
                </span>
              </div>
            ))}
            {events.length === 0 && (
              <p className="text-muted-foreground">Waiting for events...</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
