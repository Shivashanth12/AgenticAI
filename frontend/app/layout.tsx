import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "Orbit · URL Engineering",
  description: "Short links and governed engineering workflows",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
