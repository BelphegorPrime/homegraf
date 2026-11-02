import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

// The preset themes with a .dark class on <html>: follow the system, a saved choice wins (see ThemeToggle).
try {
  const saved = localStorage.getItem("homegraf-theme");
  document.documentElement.classList.toggle("dark", saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches);
} catch {
  document.documentElement.classList.toggle("dark", matchMedia("(prefers-color-scheme: dark)").matches);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={new QueryClient()}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
