import type { Metadata } from "next";
import type { ReactNode } from "react";
import { IBM_Plex_Sans_KR } from "next/font/google";
import "./globals.css";

// Plex's squared numerals keep long won amounts legible at a glance.
const plex = IBM_Plex_Sans_KR({
  weight: ["400", "500", "600", "700"],
  subsets: ["latin"],
  display: "swap",
  preload: false,
  variable: "--font-plex",
});

export const metadata: Metadata = {
  title: "FinGuard AI | 포트폴리오 대시보드",
  description: "국내 주식 계좌 조회와 포트폴리오 리스크 진단",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ko" className={plex.variable}>
      <body>{children}</body>
    </html>
  );
}
