<template>
  <svg
    class="app-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.grid" />
  </svg>
</template>

<script setup>
defineProps({
  name: { type: String, default: 'grid' },
})

const paths = {
  logo: 'M12 2.5 20 7v10l-8 4.5L4 17V7l8-4.5Zm0 0V12m8-5-8 5-8-5m8 5v9.5',
  grid: 'M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z',
  cases: 'M7 3h10v4H7V3ZM5 5H4a1 1 0 0 0-1 1v14h18V6a1 1 0 0 0-1-1h-1M7 12h10M7 16h7',
  users: 'M16 20v-1.5a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4V20m7-9.5a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm13 9.5v-1.5a4 4 0 0 0-3-3.87m-2-11.92a4 4 0 0 1 0 7.75',
  search: 'm21 21-4.35-4.35m2.35-5.15a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z',
  evaluation: 'M4 19V9m5 10V5m5 14v-7m5 7V3M2 21h20',
  shield: 'M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10Zm-3-10 2 2 4-5',
  route: 'M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm12 12a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM8.5 6H14a4 4 0 0 1 4 4v5m-6-3-3 3 3 3',
  mic: 'M12 15a4 4 0 0 0 4-4V6a4 4 0 1 0-8 0v5a4 4 0 0 0 4 4Zm7-4a7 7 0 0 1-14 0m7 7v4m-4 0h8',
  chart: 'M4 19V9m5 10V5m5 14v-7m5 7V3M2 21h20',
  activity: 'M3 12h4l2-7 4 14 2-7h6',
  settings: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm8-3.5 2-1-2-3.46-2.24.16-1.6-.93L15.5 4.6H11.5l-.66 2.17-1.6.93L7 7.54 5 11l2 1v1.85l-2 1 2 3.46 2.24-.16 1.6.93.66 2.17h4l.66-2.17 1.6-.93 2.24.16 2-3.46-2-1V12Z',
  governance: 'M4 4h16v16H4V4Zm4 4h8M8 12h8m-8 4h5',
  reconcile: 'M20 7h-9a4 4 0 0 0-4 4v1m-3 5h9a4 4 0 0 0 4-4v-1m0-8 3 3-3 3M7 14l-3 3 3 3',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'm6 6 12 12M18 6 6 18',
  logout: 'M10 17l5-5-5-5m5 5H3m9-9h7a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-7',
  chevron: 'm9 18 6-6-6-6',
  plus: 'M12 5v14M5 12h14',
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Zm-8 12h4',
  user: 'M20 21a8 8 0 0 0-16 0m8-10a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  refresh: 'M20 11a8 8 0 1 0-2.34 5.66M20 4v7h-7',
  warning: 'M12 3 2 21h20L12 3Zm0 6v5m0 3h.01',
  check: 'm5 12 4 4L19 6',
  database: 'M20 6c0 1.66-3.58 3-8 3S4 7.66 4 6s3.58-3 8-3 8 1.34 8 3Zm0 0v6c0 1.66-3.58 3-8 3s-8-1.34-8-3V6m16 6v6c0 1.66-3.58 3-8 3s-8-1.34-8-3v-6',
}
</script>
