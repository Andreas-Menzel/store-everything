<script setup lang="ts">
/**
 * A file's picture: the placeholder first, the thumbnail when it arrives, an icon when there is
 * none ([F-028](../../../../features/F-028-thumbnails-and-previews.md) FR-3, FR-5).
 *
 * The order is the point. A grid paints the placeholder from data the listing already carried, so
 * a folder of two hundred photos is coloured immediately and *then* sharpens — instead of two
 * hundred grey rectangles, or two hundred requests before anything appears. A file with nothing
 * to render never fires a request at all: the row said so.
 *
 * The placeholder is drawn as a grid of rectangles behind a blur filter rather than an image, so
 * there is nothing to decode, nothing to fetch, and no canvas. `loading="lazy"` leaves the
 * scheduling to the browser, which is better at it than any heuristic here would be.
 */
import { computed, ref } from 'vue';

import { decodePlaceholder } from './placeholder';

const props = withDefaults(
  defineProps<{
    /** The thumbnail URL, or nothing when the file has no visual source. */
    src?: string;
    /** The encoded placeholder from the listing row, if it had one. */
    placeholder?: string | null;
    alt?: string;
  }>(),
  { src: undefined, placeholder: undefined, alt: '' },
);

const loaded = ref(false);
const failed = ref(false);
const grid = computed(() => decodePlaceholder(props.placeholder));
const showsImage = computed(() => props.src !== undefined && !failed.value);
</script>

<template>
  <div
    class="relative isolate flex h-full w-full items-center justify-center overflow-hidden bg-(--color-surface-sunken)"
  >
    <svg
      v-if="grid"
      class="absolute inset-0 h-full w-full"
      :viewBox="`0 0 ${grid.columns} ${grid.rows}`"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <filter id="placeholder-blur">
        <!-- In viewBox units: one cell wide, so the twelve rectangles read as one soft image. -->
        <feGaussianBlur stdDeviation="0.6" />
      </filter>
      <g filter="url(#placeholder-blur)">
        <rect
          v-for="(cell, index) in grid.cells"
          :key="index"
          :x="index % grid.columns"
          :y="Math.floor(index / grid.columns)"
          width="1.02"
          height="1.02"
          :fill="cell"
        />
      </g>
    </svg>

    <img
      v-if="showsImage"
      :src="src"
      :alt="alt"
      loading="lazy"
      decoding="async"
      class="relative h-full w-full object-cover transition-opacity duration-200"
      :class="loaded ? 'opacity-100' : 'opacity-0'"
      @load="loaded = true"
      @error="failed = true"
    />
    <!-- Nothing to render, and nothing pretending otherwise: the type icon is the caller's,
         because only they know what kind of file this is. -->
    <span v-else-if="!grid" class="relative text-xs text-(--color-ink-muted)">
      <slot name="fallback">—</slot>
    </span>
  </div>
</template>
