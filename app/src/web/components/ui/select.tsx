import { cn } from "cn"
import { ChevronDownIcon } from "lucide-react"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

export interface SelectOption { value: string; label?: string }

/** A select built from the dropdown menu: one choice out of a list, drawn in the theme (unlike the browser's own popup). */
function Select({ value, onValueChange, options, placeholder, size = "default", disabled, className, ...props }: {
  value: string
  onValueChange: (value: string) => void
  options: SelectOption[]
  placeholder?: string
  size?: "sm" | "default"
  disabled?: boolean
  className?: string
  "aria-label"?: string
}) {
  const current = options.find((o) => o.value === value)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        data-slot="select"
        disabled={disabled}
        className={cn(
          "flex h-8 w-fit min-w-0 items-center justify-between gap-2 rounded-lg border border-input bg-transparent py-1 pr-2.5 pl-2.5 text-left text-sm whitespace-nowrap transition-colors outline-none select-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 aria-expanded:border-ring dark:bg-input/30 dark:hover:bg-input/50",
          size === "sm" && "h-7 py-0.5",
          className
        )}
        {...props}
      >
        <span className={cn("truncate", !current && "text-muted-foreground")}>{current ? (current.label ?? current.value) : (placeholder ?? "")}</span>
        <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuRadioGroup value={value} onValueChange={(v) => onValueChange(String(v))}>
          {options.map((o) => <DropdownMenuRadioItem key={o.value} value={o.value}>{o.label ?? o.value}</DropdownMenuRadioItem>)}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export { Select }
