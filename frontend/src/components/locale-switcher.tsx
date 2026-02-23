"use client";

import { useTranslations } from "use-intl";
import { Button } from "@/components/ui/button";
import { useAppLocale } from "@/components/i18n-provider";

export function LocaleSwitcher() {
  const t = useTranslations("locale");
  const { locale, setLocale } = useAppLocale();

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-muted-foreground">{t("label")}</span>
      <div className="inline-flex items-center rounded-md border border-border p-0.5">
        <Button
          variant={locale === "en" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setLocale("en")}
        >
          {t("english")}
        </Button>
        <Button
          variant={locale === "ru" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setLocale("ru")}
        >
          {t("russian")}
        </Button>
      </div>
    </div>
  );
}
