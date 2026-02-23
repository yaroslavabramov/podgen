"use client";

import { Badge } from "@/components/ui/badge";
import { useTranslations } from "use-intl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface ShownotesViewerProps {
  shownotes: Record<string, unknown> | null;
}

interface ParsedSection {
  title: string;
  references: ParsedReference[];
  conflicts: ParsedConflict[];
}

interface ParsedReference {
  claim: string;
  verification: string;
  sources: ParsedSource[];
}

interface ParsedSource {
  title: string;
  url: string;
  date: string;
  quote: string;
}

interface ParsedConflict {
  topic: string;
  positions: string[];
  reason: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function parseShownotes(payload: Record<string, unknown> | null): ParsedSection[] {
  if (!payload) return [];
  const sectionsValue = payload.sections;
  if (!Array.isArray(sectionsValue)) return [];

  return sectionsValue
    .map((section) => {
      const sectionRec = asRecord(section);
      if (!sectionRec) return null;

      const references = Array.isArray(sectionRec.references)
        ? sectionRec.references
            .map((ref) => {
              const refRec = asRecord(ref);
              if (!refRec) return null;
              const sources = Array.isArray(refRec.sources)
                ? refRec.sources
                    .map((source) => {
                      const sourceRec = asRecord(source);
                      if (!sourceRec) return null;
                      return {
                        title: asString(sourceRec.title, "Untitled source"),
                        url: asString(sourceRec.url),
                        date: asString(sourceRec.date),
                        quote: asString(sourceRec.quote),
                      };
                    })
                    .filter((item): item is ParsedSource => item !== null)
                : [];

              return {
                claim: asString(refRec.claim),
                verification: asString(refRec.verification, "UNKNOWN"),
                sources,
              };
            })
            .filter((item): item is ParsedReference => item !== null)
        : [];

      const conflicts = Array.isArray(sectionRec.conflicts)
        ? sectionRec.conflicts
            .map((conflict) => {
              const conflictRec = asRecord(conflict);
              if (!conflictRec) return null;
              return {
                topic: asString(conflictRec.topic, "Unspecified topic"),
                positions: asStringArray(conflictRec.positions),
                reason: asString(conflictRec.reason),
              };
            })
            .filter((item): item is ParsedConflict => item !== null)
        : [];

      return {
        title: asString(sectionRec.title, "Section"),
        references,
        conflicts,
      };
    })
    .filter((item): item is ParsedSection => item !== null);
}

function verificationBadgeVariant(verification: string): "success" | "warning" | "destructive" | "secondary" {
  if (verification === "VERIFIED") return "success";
  if (verification === "OBSERVED") return "warning";
  if (verification === "DISPUTED") return "destructive";
  return "secondary";
}

export function ShownotesViewer({ shownotes }: ShownotesViewerProps) {
  const t = useTranslations("shownotesViewer");
  const sections = parseShownotes(shownotes);

  if (sections.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{t("title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t("empty")}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        {sections.map((section, sectionIdx) => (
          <section key={`${section.title}-${sectionIdx}`} className="space-y-3">
            <h3 className="text-base font-semibold">{section.title}</h3>

            {section.references.length > 0 && (
              <div className="space-y-3">
                {section.references.map((ref, refIdx) => (
                  <article key={`${sectionIdx}-ref-${refIdx}`} className="rounded-lg border border-border p-3 space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium">{ref.claim || t("claimFallback")}</p>
                      <Badge variant={verificationBadgeVariant(ref.verification)}>
                        {ref.verification}
                      </Badge>
                    </div>

                    {ref.sources.length > 0 && (
                      <div className="space-y-2">
                        {ref.sources.map((source, sourceIdx) => (
                          <div key={`${sectionIdx}-ref-${refIdx}-src-${sourceIdx}`} className="rounded-md bg-muted/60 p-2 text-xs">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-semibold">{source.title}</span>
                              {source.date && <span className="text-muted-foreground">{source.date}</span>}
                              {source.url && (
                                <a
                                  href={source.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-primary hover:underline break-all"
                                >
                                  {source.url}
                                </a>
                              )}
                            </div>
                            {source.quote && <p className="mt-1 text-muted-foreground">{source.quote}</p>}
                          </div>
                        ))}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}

            {section.conflicts.length > 0 && (
              <div className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3">
                <p className="text-sm font-medium text-destructive">{t("conflicts")}</p>
                {section.conflicts.map((conflict, conflictIdx) => (
                  <div key={`${sectionIdx}-conflict-${conflictIdx}`} className="space-y-1 text-sm">
                    <p className="font-medium">{conflict.topic}</p>
                    {conflict.positions.length > 0 && (
                      <p className="text-muted-foreground">
                        {conflict.positions.join(" vs ")}
                      </p>
                    )}
                    {conflict.reason && <p className="text-muted-foreground">{conflict.reason}</p>}
                  </div>
                ))}
              </div>
            )}
          </section>
        ))}
      </CardContent>
    </Card>
  );
}
