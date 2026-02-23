"use client";

import { type CostEstimate } from "@/lib/api";
import { useTranslations } from "use-intl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface CostEstimateCardProps {
  cost: CostEstimate;
}

function formatUsd(value: number) {
  return `$${value.toFixed(2)}`;
}

export function CostEstimateCard({ cost }: CostEstimateCardProps) {
  const t = useTranslations("costEstimateCard");
  const total = cost.estimated_total || 0;
  const llmShare = total > 0 ? (cost.estimated_llm_cost / total) * 100 : 0;
  const ttsShare = total > 0 ? (cost.estimated_tts_cost / total) * 100 : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="rounded-lg border border-border p-3">
            <p className="text-xs text-muted-foreground">{t("llm")}</p>
            <p className="mt-1 text-2xl font-semibold">{formatUsd(cost.estimated_llm_cost)}</p>
          </div>
          <div className="rounded-lg border border-border p-3">
            <p className="text-xs text-muted-foreground">{t("tts")}</p>
            <p className="mt-1 text-2xl font-semibold">{formatUsd(cost.estimated_tts_cost)}</p>
          </div>
          <div className="rounded-lg border border-primary/40 bg-primary/5 p-3">
            <p className="text-xs text-muted-foreground">{t("total")}</p>
            <p className="mt-1 text-2xl font-semibold text-primary">{formatUsd(cost.estimated_total)}</p>
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>{t("split")}</span>
            <span>{t("minutes", { minutes: cost.estimated_minutes })}</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full bg-primary"
              style={{ width: `${Math.max(0, Math.min(100, llmShare))}%` }}
            />
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full bg-accent"
              style={{ width: `${Math.max(0, Math.min(100, ttsShare))}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>{t("llmShare", { pct: Math.round(llmShare) })}</span>
            <span>{t("ttsShare", { pct: Math.round(ttsShare) })}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
