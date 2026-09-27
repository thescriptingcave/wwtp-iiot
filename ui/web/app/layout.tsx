import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'WWTP operator dashboard',
  description:
    'Live view of the wastewater treatment plant. Generated from contracts/tags.yaml.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <a className="topbar__brand" href="/">
            WWTP
          </a>
          <nav className="topbar__nav">
            <a href="/">Overview</a>
            <a href="/permit">Permit</a>
            <a href="/alarms">Alarms</a>
          </nav>
          <span className="topbar__note">
            generated from <code>contracts/tags.yaml</code>
          </span>
        </header>
        <main className="page">{children}</main>
        <footer className="foot">
          <p>
            A red band is &ldquo;outside the contract&rsquo;s normal band&rdquo;, not an
            alarm. <code>alarms/rules.py</code> decides what is wrong, from a measured
            healthy distribution. See <code>docs/ALARM-TUNING.md</code>.
          </p>
        </footer>
      </body>
    </html>
  );
}
