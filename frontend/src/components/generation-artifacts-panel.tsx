"use client";

import { useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import type { GenerationArtifacts } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type ArtifactTab = "plan" | "beats" | "screenplay";

interface GenerationArtifactsPanelProps {
  artifacts: GenerationArtifacts | null;
  loading: boolean;
  error: string | null;
}

export function GenerationArtifactsPanel({
  artifacts,
  loading,
  error,
}: GenerationArtifactsPanelProps) {
  const t = useTranslations("generatePage");
  const [tab, setTab] = useState<ArtifactTab>("plan");

  const hasAnyArtifacts = useMemo(() => {
    if (!artifacts) return false;
    return (
      artifacts.episode_plan.length > 0 ||
      artifacts.beat_sheets.length > 0 ||
      artifacts.screenplay.length > 0
    );
  }, [artifacts]);

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>{t("artifactsTitle")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant={tab === "plan" ? "default" : "outline"}
            size="sm"
            onClick={() => setTab("plan")}
          >
            {t("artifactsPlanTab")}
          </Button>
          <Button
            type="button"
            variant={tab === "beats" ? "default" : "outline"}
            size="sm"
            onClick={() => setTab("beats")}
          >
            {t("artifactsBeatsTab")}
          </Button>
          <Button
            type="button"
            variant={tab === "screenplay" ? "default" : "outline"}
            size="sm"
            onClick={() => setTab("screenplay")}
          >
            {t("artifactsScreenplayTab")}
          </Button>
        </div>

        {loading && !hasAnyArtifacts && (
          <p className="text-sm text-muted-foreground">{t("artifactsLoading")}</p>
        )}
        {error && (
          <p className="text-sm text-destructive">{error}</p>
        )}
        {!loading && !error && !hasAnyArtifacts && (
          <p className="text-sm text-muted-foreground">{t("artifactsEmpty")}</p>
        )}

        {hasAnyArtifacts && tab === "plan" && (
          <div className="space-y-4">
            {artifacts?.episode_plan.length ? (
              artifacts.episode_plan.map((act) => (
                <div key={act.act_number} className="rounded-md border border-border p-4 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{t("artifactsActLabel", { act: act.act_number })}</Badge>
                    {act.story_role && <Badge variant="secondary">{act.story_role}</Badge>}
                  </div>
                  {act.act_title && <h4 className="text-base font-semibold">{act.act_title}</h4>}
                  {act.purpose && <p className="text-sm">{act.purpose}</p>}
                  {act.internal_arc && (
                    <p className="text-sm text-muted-foreground">
                      {t("artifactsInternalArc")}: {act.internal_arc}
                    </p>
                  )}
                  {act.opening_question && (
                    <p className="text-sm">
                      <span className="text-muted-foreground">{t("artifactsOpeningQuestion")}:</span>{" "}
                      {act.opening_question}
                    </p>
                  )}
                  {act.ending_turn && (
                    <p className="text-sm">
                      <span className="text-muted-foreground">{t("artifactsEndingTurn")}:</span>{" "}
                      {act.ending_turn}
                    </p>
                  )}
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("artifactsPlanEmpty")}</p>
            )}
          </div>
        )}

        {hasAnyArtifacts && tab === "beats" && (
          <div className="space-y-4">
            {artifacts?.beat_sheets.length ? (
              artifacts.beat_sheets.map((act) => (
                <div key={act.act_number} className="rounded-md border border-border p-4 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">{t("artifactsActLabel", { act: act.act_number })}</Badge>
                    {act.story_role && <Badge variant="secondary">{act.story_role}</Badge>}
                  </div>
                  {act.act_title && <h4 className="text-base font-semibold">{act.act_title}</h4>}
                  <div className="space-y-3">
                    {act.scenes.map((scene) => (
                      <div key={scene.scene_number} className="rounded-md bg-muted/40 p-3 space-y-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="outline">
                            {t("artifactsSceneLabel", { scene: scene.scene_number })}
                          </Badge>
                          {scene.scene_type && <Badge variant="secondary">{scene.scene_type}</Badge>}
                          {scene.scene_job && <Badge variant="secondary">{scene.scene_job}</Badge>}
                        </div>
                        {scene.description && <p className="text-sm">{scene.description}</p>}
                        {scene.transition_note && (
                          <p className="text-sm text-muted-foreground">
                            {t("artifactsTransitionNote")}: {scene.transition_note}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("artifactsBeatsEmpty")}</p>
            )}
          </div>
        )}

        {hasAnyArtifacts && tab === "screenplay" && (
          <div className="space-y-4">
            {artifacts?.screenplay.length ? (
              artifacts.screenplay.map((act) => (
                <div key={act.act_number} className="rounded-md border border-border p-4 space-y-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{t("artifactsActLabel", { act: act.act_number })}</Badge>
                  </div>
                  <div className="space-y-4">
                    {act.scenes.map((scene) => (
                      <div key={scene.scene_number} className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Badge variant="secondary">
                            {t("artifactsSceneLabel", { scene: scene.scene_number })}
                          </Badge>
                        </div>
                        <div className="space-y-2 rounded-md bg-muted/40 p-3">
                          {scene.dialogue.map((line, index) => (
                            <div key={index} className="flex gap-3">
                              <span className="w-5 shrink-0 text-sm font-semibold text-primary">
                                {line.speaker}
                              </span>
                              <p className="text-sm">{line.text}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">{t("artifactsScreenplayEmpty")}</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
