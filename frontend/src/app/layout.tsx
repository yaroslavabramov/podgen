import type { Metadata } from "next";
import "./globals.css";
import { I18nProvider } from "@/components/i18n-provider";

export const metadata: Metadata = {
  title: "Podgen — AI Podcast Generation",
  description: "Generate long-form, research-backed podcasts with AI",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-background text-foreground antialiased">
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}
