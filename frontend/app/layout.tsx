import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RiskPulse",
  description: "Portfolio and macro risk dashboard"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="surface-light" suppressHydrationWarning>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var m=localStorage.getItem("riskpulse_surface_mode")||"light";document.body.classList.toggle("surface-light",m!=="dark")}catch(e){document.body.classList.add("surface-light")}`
          }}
        />
        {children}
      </body>
    </html>
  );
}
