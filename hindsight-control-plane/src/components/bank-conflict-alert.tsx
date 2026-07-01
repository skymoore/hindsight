"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Copy } from "lucide-react";
import { toast } from "sonner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/**
 * Renders an AMBIGUOUS_BANK_ID error: a bare bank id that resolves to more than
 * one accessible schema. Lists each fully-qualified conflict as a copyable item
 * so the user can disambiguate by referring to a bank by its qualified id.
 */
export function BankConflictAlert({ conflicts }: { conflicts: string[] }) {
  const t = useTranslations("bankConflict");

  if (!conflicts || conflicts.length === 0) return null;

  return (
    <Alert variant="destructive">
      <AlertTriangle className="h-4 w-4" />
      <AlertTitle>{t("title")}</AlertTitle>
      <AlertDescription>
        <p className="mb-2">{t("body")}</p>
        <ul className="space-y-1">
          {conflicts.map((id) => (
            <li key={id}>
              <button
                type="button"
                title={t("copy")}
                aria-label={t("copy")}
                className="inline-flex items-center gap-1.5 rounded bg-destructive/10 px-2 py-1 font-mono text-xs hover:bg-destructive/20 transition-colors"
                onClick={() => {
                  navigator.clipboard.writeText(id).then(
                    () => toast.success(t("copy")),
                    () => toast.error(t("copy"))
                  );
                }}
              >
                <span>{id}</span>
                <Copy className="h-3 w-3 shrink-0" />
              </button>
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}
