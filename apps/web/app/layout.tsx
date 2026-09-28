import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
export const metadata: Metadata = {title:"ProcureFlow · 采购工作台",description:"Evidence-first procurement alpha"};
export default function RootLayout({children}:{children:ReactNode}) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
