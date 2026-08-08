import clsx, { type ClassValue } from 'clsx'

/** Conditional class names. Thin alias so swapping the implementation stays a one-liner. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs)
}
