"use client";

import { useMemo, useState } from "react";
import type { GenerationArtifacts, V2ArtifactItem, V2Artifacts } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type V2Tab = "research" | "treatment" | "draft" | "critique" | "claims" | "final" | "compare";

interface GenerationV2ArtifactsPanelProps {
  artifacts: V2Artifacts | null;
  loading: boolean;
  error: string | null;
  isCompare: boolean;
  onPromoteAudio?: (variant?: "v1" | "v2") => void;
  promoting?: boolean;
}

const tabTypes: Record<V2Tab, string[]> = {
  research: ["source_card", "quote_card", "editorial_atom", "research_memo", "research_audit"],
  treatment: ["treatment", "act_dossier"],
  draft: ["act_draft", "continuity_ledger"],
  critique: ["episode_critique"],
  claims: ["draft_claim", "citation_link", "fact_issue"],
  final: ["final_transcript"],
  compare: [],
};

export function GenerationV2ArtifactsPanel({
  artifacts,
  loading,
  error,
  isCompare,
  onPromoteAudio,
  promoting,
}: GenerationV2ArtifactsPanelProps) {
  const [tab, setTab] = useState<V2Tab>(isCompare ? "compare" : "research");

  const hasAny = useMemo(() => {
    if (!artifacts) return false;
    return Object.values(artifacts.artifacts).some((items) => items.length > 0) ||
      Boolean(artifacts.compare_v1_artifacts) ||
      Boolean(artifacts.compare_v2_artifacts && Object.keys(artifacts.compare_v2_artifacts).length > 0);
  }, [artifacts]);

  const visibleItems = artifacts ? collectItems(artifacts.artifacts, tabTypes[tab]) : [];
  const compareV2Items = artifacts?.compare_v2_artifacts
    ? collectItems(artifacts.compare_v2_artifacts, ["research_memo", "treatment", "act_draft", "episode_critique", "final_transcript"])
    : [];

  return (
    <Card className="mt-6">
      <CardHeader>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <CardTitle>Generation V2 Artifacts</CardTitle>
          {onPromoteAudio && !isCompare && (
            <Button type="button" size="sm" onClick={() => onPromoteAudio()} disabled={promoting}>
              {promoting ? "Promoting..." : "Promote to audio"}
            </Button>
          )}
          {onPromoteAudio && isCompare && (
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => onPromoteAudio("v1")} disabled={promoting}>
                {promoting ? "Promoting..." : "Promote V1"}
              </Button>
              <Button type="button" size="sm" onClick={() => onPromoteAudio("v2")} disabled={promoting}>
                {promoting ? "Promoting..." : "Promote V2"}
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          {(["research", "treatment", "draft", "critique", "claims", "final"] as V2Tab[]).map((value) => (
            <Button
              key={value}
              type="button"
              variant={tab === value ? "default" : "outline"}
              size="sm"
              onClick={() => setTab(value)}
            >
              {label(value)}
            </Button>
          ))}
          {isCompare && (
            <Button
              type="button"
              variant={tab === "compare" ? "default" : "outline"}
              size="sm"
              onClick={() => setTab("compare")}
            >
              Compare
            </Button>
          )}
        </div>

        {loading && !hasAny && <p className="text-sm text-muted-foreground">Loading V2 artifacts...</p>}
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!loading && !error && !hasAny && (
          <p className="text-sm text-muted-foreground">No V2 artifacts yet.</p>
        )}

        {tab === "compare" && isCompare ? (
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-md border border-border p-4">
              <div className="mb-3 flex items-center gap-2">
                <Badge variant="outline">V1</Badge>
                <span className="text-sm text-muted-foreground">Current pipeline draft</span>
              </div>
              <V1Summary artifacts={artifacts?.compare_v1_artifacts || null} />
            </div>
            <div className="rounded-md border border-border p-4">
              <div className="mb-3 flex items-center gap-2">
                <Badge variant="outline">V2</Badge>
                <span className="text-sm text-muted-foreground">Editorial agent draft</span>
              </div>
              <ArtifactList items={compareV2Items} />
            </div>
          </div>
        ) : (
          <ArtifactList items={visibleItems} />
        )}
      </CardContent>
    </Card>
  );
}

function collectItems(groups: Record<string, V2ArtifactItem[]>, types: string[]): V2ArtifactItem[] {
  return types.flatMap((type) => groups[type] || []);
}

function label(tab: V2Tab): string {
  const labels: Record<V2Tab, string> = {
    research: "Research",
    treatment: "Treatment",
    draft: "Act Drafts",
    critique: "Critique",
    claims: "Claims",
    final: "Final",
    compare: "Compare",
  };
  return labels[tab];
}

function ArtifactList({ items }: { items: V2ArtifactItem[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing available for this tab yet.</p>;
  }
  return (
    <div className="space-y-3">
      {items.map((item) => (
        <div key={item.id} className="rounded-md border border-border p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{item.artifact_key}</Badge>
            <Badge variant={item.status === "failed" ? "destructive" : "outline"}>{item.status}</Badge>
            {item.model && <Badge variant="outline">{item.model}</Badge>}
          </div>
          <ArtifactPayload payload={item.payload} />
        </div>
      ))}
    </div>
  );
}

function ArtifactPayload({ payload }: { payload: Record<string, unknown> }) {
  const title = stringValue(payload.title) || stringValue(payload.verdict) || stringValue(payload.summary);
  const description =
    stringValue(payload.thesis) ||
    stringValue(payload.logline) ||
    stringValue(payload.explanation) ||
    stringValue(payload.description) ||
    stringValue(payload.statement) ||
    stringValue(payload.text);
  const lines = Array.isArray(payload.lines) ? payload.lines : null;
  const acts = Array.isArray(payload.acts) ? payload.acts : null;

  return (
    <div className="space-y-2">
      {title && <p className="text-sm font-medium">{title}</p>}
      {description && <p className="text-sm text-muted-foreground">{description}</p>}
      {acts && (
        <div className="space-y-1">
          {acts.slice(0, 8).map((act, index) => (
            <p key={index} className="text-sm">
              <span className="text-muted-foreground">Act {index + 1}: </span>
              {stringValue((act as Record<string, unknown>).title) || JSON.stringify(act).slice(0, 160)}
            </p>
          ))}
        </div>
      )}
      {lines && (
        <div className="space-y-1 rounded-md bg-muted/40 p-3">
          {lines.slice(0, 12).map((line, index) => {
            const linePayload = line as Record<string, unknown>;
            return (
              <div key={index} className="flex gap-2 text-sm">
                <span className="w-5 shrink-0 font-semibold text-primary">{stringValue(linePayload.speaker)}</span>
                <span>{stringValue(linePayload.text)}</span>
              </div>
            );
          })}
          {lines.length > 12 && <p className="text-xs text-muted-foreground">+ {lines.length - 12} more lines</p>}
        </div>
      )}
    </div>
  );
}

function V1Summary({ artifacts }: { artifacts: GenerationArtifacts | null }) {
  if (!artifacts) return <p className="text-sm text-muted-foreground">V1 branch has not finished yet.</p>;
  return (
    <div className="space-y-3 text-sm">
      <p>
        <span className="text-muted-foreground">Episode plan acts:</span> {artifacts.episode_plan.length}
      </p>
      <p>
        <span className="text-muted-foreground">Beat sheet acts:</span> {artifacts.beat_sheets.length}
      </p>
      <p>
        <span className="text-muted-foreground">Screenplay acts:</span> {artifacts.screenplay.length}
      </p>
      {artifacts.screenplay.slice(0, 2).map((act) => (
        <div key={act.act_number} className="rounded-md bg-muted/40 p-3">
          <Badge variant="outline">Act {act.act_number}</Badge>
          {act.scenes.slice(0, 2).map((scene) => (
            <div key={scene.scene_number} className="mt-2 space-y-1">
              {scene.dialogue.slice(0, 4).map((line, index) => (
                <p key={index}>
                  <span className="font-medium text-primary">{line.speaker}: </span>
                  {line.text}
                </p>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
