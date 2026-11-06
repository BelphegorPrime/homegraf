import { MoonIcon, SunIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";

/** Light or dark: follows the system until you choose, then your choice is kept in this browser. */
export function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const flip = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("homegraf-theme", next ? "dark" : "light");
    } catch {
      /* the choice only lasts for this page */
    }
    setDark(next);
  };
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={flip}
      aria-label={dark ? "Switch to the light theme" : "Switch to the dark theme"}
      title={dark ? "Light theme" : "Dark theme"}
    >
      {dark ? <SunIcon /> : <MoonIcon />}
    </Button>
  );
}
