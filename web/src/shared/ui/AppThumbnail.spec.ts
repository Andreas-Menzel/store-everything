import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import AppThumbnail from './AppThumbnail.vue';
import { decodePlaceholder } from './placeholder';

/**
 * The placeholder codec and the three states of a file's picture
 * ([F-028/FR-3, FR-5](../../../../features/F-028-thumbnails-and-previews.md)).
 *
 * The codec's tests are the interesting ones: it reads bytes produced by a Python extractor, so
 * the two implementations agreeing is a property no type system checks. The fixture below is the
 * real output of `preview-gen` for the corpus image — red half, blue half, 800x400 — so a change
 * on either side of that boundary fails here.
 */

/** `preview-gen`'s placeholder for `corpus/fixtures/images/two-tone.png`. */
const TWO_TONE = 'AQQDAyABkM4fJcIiLSdAmRtDoc4fJcIiLSdAmRtDoc4fJcIiLSdAmRtDoQ';

describe('decodePlaceholder', () => {
  it('reads what the extractor wrote', { tags: ['@F-028/FR-5'] }, () => {
    const decoded = decodePlaceholder(TWO_TONE);

    expect(decoded).toBeDefined();
    expect(decoded?.columns).toBe(4);
    expect(decoded?.rows).toBe(3);
    // The source's dimensions, so a cell reserves the right space before any image arrives.
    expect(decoded?.width).toBe(800);
    expect(decoded?.height).toBe(400);
    expect(decoded?.cells).toHaveLength(12);
    // Left half red, right half blue — the fixture's own truth, seen through 43 bytes.
    expect(decoded?.cells[0]).toMatch(/^rgb\(2\d\d /);
    expect(decoded?.cells[3]).toMatch(/^rgb\(\d+ \d+ 1\d\d\)$/);
  });

  it('treats anything it does not understand as absent', { tags: ['@F-028/FR-5'] }, () => {
    // A placeholder is a nicety. Throwing on a malformed one would turn a cosmetic surprise
    // into a broken page, so every bad input is simply "no placeholder".
    for (const bad of [undefined, null, '', 'not base64!!', 'AQ', btoa('\x09\x04\x03')]) {
      expect(decodePlaceholder(bad)).toBeUndefined();
    }
  });
});

describe('AppThumbnail', () => {
  it('paints the placeholder before any image arrives', { tags: ['@F-028/FR-5'] }, () => {
    const wrapper = mount(AppThumbnail, { props: { placeholder: TWO_TONE, src: '/thumb.webp' } });

    expect(wrapper.findAll('rect')).toHaveLength(12);
    // The image is there but transparent until it loads, so the placeholder is what is seen.
    const image = wrapper.get('img');
    expect(image.classes()).toContain('opacity-0');

    image.trigger('load');
    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.get('img').classes()).toContain('opacity-100');
    });
  });

  it('shows the caller’s icon when there is nothing to render', { tags: ['@F-028/FR-3'] }, () => {
    const wrapper = mount(AppThumbnail, { slots: { fallback: 'PDF' } });

    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('svg').exists()).toBe(false);
    expect(wrapper.text()).toBe('PDF');
  });

  it('falls back when the thumbnail itself fails', { tags: ['@F-028/FR-3'] }, async () => {
    const wrapper = mount(AppThumbnail, {
      props: { src: '/gone.webp' },
      slots: { fallback: 'PDF' },
    });

    await wrapper.get('img').trigger('error');

    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.text()).toBe('PDF');
  });
});
