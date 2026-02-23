"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { IntlProvider } from "use-intl";
import enMessages from "@/i18n/messages/en.json";
import ruMessages from "@/i18n/messages/ru.json";

export type AppLocale = "en" | "ru";

interface LocaleContextValue {
  locale: AppLocale;
  setLocale: (locale: AppLocale) => void;
}

const LocaleContext = createContext<LocaleContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<AppLocale>("en");

  useEffect(() => {
    const saved = localStorage.getItem("locale");
    if (saved === "en" || saved === "ru") {
      setLocale(saved);
      return;
    }

    const fromNavigator = navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
    setLocale(fromNavigator);
  }, []);

  useEffect(() => {
    localStorage.setItem("locale", locale);
    document.cookie = `locale=${locale}; path=/; max-age=31536000`;
    document.documentElement.lang = locale;
  }, [locale]);

  const messages = locale === "ru" ? ruMessages : enMessages;
  const value = useMemo(() => ({ locale, setLocale }), [locale]);

  return (
    <LocaleContext.Provider value={value}>
      <IntlProvider locale={locale} messages={messages}>
        {children}
      </IntlProvider>
    </LocaleContext.Provider>
  );
}

export function useAppLocale() {
  const context = useContext(LocaleContext);
  if (!context) throw new Error("useAppLocale must be used within I18nProvider");
  return context;
}
