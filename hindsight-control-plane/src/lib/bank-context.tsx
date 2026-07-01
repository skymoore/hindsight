"use client";

import React, { createContext, useContext, useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import { client } from "./api";

export interface BankInfo {
  bank_id: string;
  name: string | null;
  mission: string | null;
  created_at: string | null;
  updated_at: string | null;
  fact_count: number;
  last_document_at: string | null;
  visibility?: "private" | "shared";
  isOwner?: boolean;
  /** GitHub username of the owner, when the caller is allowed to see it. */
  ownerLogin?: string | null;
}

interface BankContextType {
  currentBank: string | null;
  setCurrentBank: (bank: string | null) => void;
  banks: string[];
  bankInfos: BankInfo[];
  banksLoading: boolean;
  loadBanks: () => Promise<void>;
}

const BankContext = createContext<BankContextType | undefined>(undefined);

export function BankProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [currentBank, setCurrentBank] = useState<string | null>(null);
  const [bankInfos, setBankInfos] = useState<BankInfo[]>([]);
  const [banksLoading, setBanksLoading] = useState(true);

  const loadBanks = async () => {
    setBanksLoading(true);
    try {
      const response = await client.listBanks();
      const infos: BankInfo[] =
        response.banks?.map((bank: any) => ({
          bank_id: bank.bank_id,
          name: bank.name ?? null,
          mission: bank.mission ?? null,
          created_at: bank.created_at ?? null,
          updated_at: bank.updated_at ?? null,
          fact_count: bank.fact_count ?? 0,
          last_document_at: bank.last_document_at ?? null,
        })) || [];

      // Best-effort merge of per-bank visibility. The visibility feature may be
      // disabled on the dataplane, in which case we simply leave it undefined.
      try {
        const visibilityResponse = await client.listBankVisibility();
        const visibilityById = new Map(
          (visibilityResponse.banks ?? []).map((b) => [b.bank_id, b])
        );
        for (const info of infos) {
          const v = visibilityById.get(info.bank_id);
          if (v) {
            info.visibility = v.visibility;
            info.isOwner = v.is_owner;
            // owner_login is only present when the caller may see it (admins for
            // any bank; everyone for shared banks). Undefined/null otherwise.
            info.ownerLogin = v.owner_login ?? null;
          }
        }
      } catch {
        // Visibility feature unavailable — leave visibility undefined.
      }

      setBankInfos(infos);
    } catch (error) {
      console.error("Error loading banks:", error);
    } finally {
      setBanksLoading(false);
    }
  };

  // Derive bank IDs for backwards compatibility
  const banks = bankInfos.map((b) => b.bank_id);

  // Initialize bank from URL on mount
  useEffect(() => {
    const bankMatch = pathname?.match(/^\/banks\/([^/?]+)/);
    if (bankMatch) {
      setCurrentBank(decodeURIComponent(bankMatch[1]));
    }
  }, [pathname]);

  useEffect(() => {
    loadBanks();
  }, []);

  return (
    <BankContext.Provider
      value={{ currentBank, setCurrentBank, banks, bankInfos, banksLoading, loadBanks }}
    >
      {children}
    </BankContext.Provider>
  );
}

export function useBank() {
  const context = useContext(BankContext);
  if (context === undefined) {
    throw new Error("useBank must be used within a BankProvider");
  }
  return context;
}
