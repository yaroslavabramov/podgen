"use client";


import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "use-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { LocaleSwitcher } from "@/components/locale-switcher";

export default function LoginPage() {
  const t = useTranslations("loginPage");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [isRegistering, setIsRegistering] = useState(false);
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // For dev: create/login user and store JWT
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/auth/dev-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, name: name || email.split("@")[0] }),
      });
      if (res.ok) {
        const data = await res.json();
        localStorage.setItem("token", data.token);
        router.push("/projects");
      }
    } catch {
      // Handle error
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="absolute top-4 right-4">
        <LocaleSwitcher />
      </div>
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">{t("title")}</CardTitle>
          <CardDescription>{t("subtitle")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">{t("email")}</label>
              <Input
                type="email"
                placeholder={t("emailPlaceholder")}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            {isRegistering && (
              <div className="space-y-2">
                <label className="text-sm font-medium">{t("name")}</label>
                <Input
                  placeholder={t("namePlaceholder")}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
            )}
            <Button type="submit" className="w-full">
              {isRegistering ? t("createAccount") : t("signIn")}
            </Button>
          </form>

          <div className="mt-4 text-center">
            <button
              onClick={() => setIsRegistering(!isRegistering)}
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              {isRegistering ? t("alreadyHaveAccount") : t("needAccount")}
            </button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
