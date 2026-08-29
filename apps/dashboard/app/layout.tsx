import type { Metadata } from "next";
import { headers } from "next/headers";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const sans = Geist({ variable: "--font-sans", subsets: ["latin"] });
const mono = Geist_Mono({ variable: "--font-mono", subsets: ["latin"] });

export async function generateMetadata(): Promise<Metadata> {
  const incoming = await headers();
  const host = incoming.get("host") ?? "localhost:3001";
  const protocol = incoming.get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  const image = `${protocol}://${host}/og.png`;
  return {
    title: "OpsPilot · 智能运维控制台",
    description: "多 Agent 协作的异常检测、根因分析与安全处置控制台。",
    icons: { icon: "/favicon.svg" },
    openGraph: { title: "OpsPilot", description: "Multi-Agent AIOps", images: [image] },
    twitter: { card: "summary_large_image", title: "OpsPilot", description: "Multi-Agent AIOps", images: [image] },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body className={`${sans.variable} ${mono.variable}`}>{children}</body></html>;
}
