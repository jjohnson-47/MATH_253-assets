#!/usr/bin/env python3

import argparse
import logging
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Tuple, List, Optional, Any

from PIL import Image, ImageChops, ImageFile, UnidentifiedImageError, ImageOps, ImageFilter

ImageFile.LOAD_TRUNCATED_IMAGES = True
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(processName)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

YT_TARGET_WIDTH = 1280
YT_TARGET_HEIGHT = 720
YT_MAX_SIZE_MB = 2.0

def worker_function_unpacker(args_tuple: Tuple[Any, ...]) -> Optional[Path]:
    return clean_and_resize_thumbnail(*args_tuple)

def get_pil_resampling_filter(filter_name: str):
    if hasattr(Image, "Resampling"): # Pillow 9.0.0+
        filter_map = {
            "nearest": Image.Resampling.NEAREST, "lanczos": Image.Resampling.LANCZOS,
            "bicubic": Image.Resampling.BICUBIC, "bilinear": Image.Resampling.BILINEAR,
            "box": Image.Resampling.BOX, "hamming": Image.Resampling.HAMMING,
        }
        return filter_map.get(filter_name.lower(), Image.Resampling.LANCZOS)
    else: # Older Pillow versions
        filter_map = {
            "nearest": Image.NEAREST, "lanczos": Image.LANCZOS,
            "bicubic": Image.BICUBIC, "bilinear": Image.BILINEAR,
        }
        return filter_map.get(filter_name.lower(), Image.LANCZOS)

def determine_background_for_diff(image: Image.Image, strategy: str = "top_left_pixel") -> Tuple[int, int, int]:
    if strategy == "top_left_pixel":
        try: return image.getpixel((0, 0))
        except Exception as e: logger.warning(f"[{image.filename if hasattr(image, 'filename') else 'Image'}] Could not get top-left pixel, defaulting to white. Error: {e}"); return (255, 255, 255)
    elif strategy == "white": return (255, 255, 255)
    elif strategy == "black": return (0, 0, 0)
    return (255, 255, 255)

def clean_and_resize_thumbnail(
    image_path: Path, output_dir: Path, target_width: int, target_height: int,
    output_format: str, quality: int, canvas_bg_color: str, diff_bg_strategy: str,
    threshold_value: int, skip_if_all_bg: bool, resample_filter_name: str,
    debug_save: bool = False, apply_dilation: bool = False,
    min_crop_width_ratio: Optional[float] = None, safety_border: int = 2, # Renamed from safety_border_crop
    bbox_padding: int = 20 # New: padding around tight bbox
) -> Optional[Path]:
    try:
        img = Image.open(image_path).convert("RGB")
        logger.debug(f"[{image_path.name}] Opened. Original: ({img.width}, {img.height})")
    except Exception as e:
        logger.error(f"[{image_path.name}] Error opening: {e}", exc_info=True); return None

    bg_color_for_diff = determine_background_for_diff(img, diff_bg_strategy)
    bg_image_for_diff = Image.new("RGB", img.size, bg_color_for_diff)
    diff = ImageChops.difference(img, bg_image_for_diff)
    mask = ImageOps.grayscale(diff).point(lambda p: 255 if p > threshold_value else 0, mode='1')
    logger.debug(f"[{image_path.name}] Initial mask created (threshold: {threshold_value}).")

    if apply_dilation:
        try: mask = mask.filter(ImageFilter.MaxFilter(3)); logger.debug(f"[{image_path.name}] Applied dilation.")
        except Exception as e: logger.warning(f"[{image_path.name}] Dilation failed: {e}", exc_info=True)

    bbox_from_mask = None # This will be the "tight" bbox
    can_do_safety_crop = (safety_border > 0 and mask.width > 2 * safety_border and mask.height > 2 * safety_border)
    if can_do_safety_crop:
        sub_region = (safety_border, safety_border, mask.width - safety_border, mask.height - safety_border)
        sub_mask = mask.crop(sub_region)
        bbox_in_sub = sub_mask.getbbox()
        if bbox_in_sub:
            bbox_from_mask = (bbox_in_sub[0] + safety_border, bbox_in_sub[1] + safety_border,
                              bbox_in_sub[2] + safety_border, bbox_in_sub[3] + safety_border)
            logger.info(f"[{image_path.name}] BBox from safety-cropped mask (border:{safety_border}px, adj): {bbox_from_mask}")
        else:
            logger.warning(f"[{image_path.name}] No BBox in safety-cropped region. Will try full mask.")
    elif safety_border > 0:
        logger.warning(f"[{image_path.name}] Mask too small for safety-crop. Will try full mask.")
    
    if bbox_from_mask is None:
        bbox_from_mask = mask.getbbox()
        if bbox_from_mask: logger.info(f"[{image_path.name}] BBox from full mask: {bbox_from_mask}")

    if debug_save:
        debug_mask_path = output_dir / f"{image_path.stem}_debug_mask.png"
        try: mask.save(debug_mask_path); logger.info(f"[{image_path.name}] Saved debug MASK to {debug_mask_path}")
        except Exception as e: logger.error(f"[{image_path.name}] Failed to save debug mask: {e}", exc_info=True)
    
    # --- BBox finalization, padding, and cropping ---
    final_crop_bbox = None
    if bbox_from_mask:
        box_w = bbox_from_mask[2] - bbox_from_mask[0]
        box_h = bbox_from_mask[3] - bbox_from_mask[1]
        logger.info(f"[{image_path.name}] Tight bbox from mask w×h = {box_w}×{box_h} at {bbox_from_mask}")

        # Apply optional padding (P)
        P = bbox_padding
        padded_bbox = (max(bbox_from_mask[0] - P, 0),
                       max(bbox_from_mask[1] - P, 0),
                       min(bbox_from_mask[2] + P, img.width),
                       min(bbox_from_mask[3] + P, img.height))
        logger.info(f"[{image_path.name}] BBox after padding (P={P}): {padded_bbox}")
        final_crop_bbox = padded_bbox
    else: # No bbox_from_mask found
        logger.warning(f"[{image_path.name}] No BBox found from mask operations.")
        # Fallback logic for 'if not final_crop_bbox' further down will handle this.

    # Apply min_crop_width_ratio guard if a bbox exists
    if final_crop_bbox and min_crop_width_ratio is not None:
        current_bbox_width = final_crop_bbox[2] - final_crop_bbox[0]
        min_allowed_width = int(img.width * min_crop_width_ratio) # Changed target_width to img.width for ratio calc
        if current_bbox_width < min_allowed_width:
            logger.warning(f"[{image_path.name}] Padded BBox width {current_bbox_width}px < min allowed {min_allowed_width}px (ratio of original img width). Expanding to full img width.")
            final_crop_bbox = (0, final_crop_bbox[1], img.width, final_crop_bbox[3]) # Keep vertical crop from padded_bbox
            logger.info(f"[{image_path.name}] New BBox after width expansion: {final_crop_bbox}")

    if not final_crop_bbox: # Master fallback if no bbox could be determined at all
        if skip_if_all_bg: logger.info(f"[{image_path.name}] Skipping (no BBox, skip_all_bg True)."); return None
        else: logger.warning(f"[{image_path.name}] No BBox found by any method. Using entire image."); final_crop_bbox = (0, 0, img.width, img.height)
    
    cropped_img = img.crop(final_crop_bbox)
    logger.debug(f"[{image_path.name}] Cropped using final_crop_bbox {final_crop_bbox}. Result dims: ({cropped_img.width}, {cropped_img.height})")

    if debug_save:
        debug_crop_path = output_dir / f"{image_path.stem}_debug_cropped_before_thumb.png"
        try: cropped_img.save(debug_crop_path); logger.info(f"[{image_path.name}] Saved debug CROPPED to {debug_crop_path}")
        except Exception as e: logger.error(f"[{image_path.name}] Failed to save debug cropped: {e}", exc_info=True)

    # --- Conditional Scaling (only scale down if bigger than target) ---
    if cropped_img.width > target_width or cropped_img.height > target_height:
        resample_filter = get_pil_resampling_filter(resample_filter_name)
        cropped_img.thumbnail((target_width, target_height), resample_filter) # In-place
        logger.debug(f"[{image_path.name}] Crop was larger than target, down-scaled to: ({cropped_img.width}×{cropped_img.height})")
    else:
        logger.debug(f"[{image_path.name}] Crop already fits within target dimensions ({cropped_img.width}×{cropped_img.height}); no scaling applied.")
    # --- End of Conditional Scaling ---

    final_canvas = Image.new("RGB", (target_width, target_height), canvas_bg_color)
    paste_x = (target_width - cropped_img.width) // 2
    paste_y = (target_height - cropped_img.height) // 2
    logger.info(f"[{image_path.name}] Target:({target_width}x{target_height}). Paste:x={paste_x},y={paste_y} for crop:({cropped_img.width}x{cropped_img.height})")
    final_canvas.paste(cropped_img, (paste_x, paste_y))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{image_path.stem}_processed.{output_format.lower()}"
    try:
        save_opts = {'format':output_format.upper(),'quality':quality,'optimize':True,'progressive':True} if output_format.upper()=="JPEG" else {'format':output_format.upper(),'optimize':True}
        final_canvas.save(output_path, **save_opts)
        logger.info(f"[{image_path.name}] Successfully processed -> {output_path.name}")
        if output_path.stat().st_size/(1024*1024) > YT_MAX_SIZE_MB: logger.warning(f"[{image_path.name}] '{output_path.name}' > {YT_MAX_SIZE_MB}MB.")
        return output_path
    except Exception as e:
        logger.error(f"[{image_path.name}] Error saving: {e}", exc_info=True); return None

def main():
    parser = argparse.ArgumentParser(description="Batch process thumbnails for prominence.", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("input_dir", type=str, help="Input directory.")
    parser.add_argument("output_dir", type=str, help="Output directory.")
    parser.add_argument("--glob_pattern", type=str, default="*.jpg,*.jpeg,*.png", help="Input file patterns.")
    parser.add_argument("--width", type=int, default=YT_TARGET_WIDTH, help=f"Target width (default: {YT_TARGET_WIDTH}).")
    parser.add_argument("--height", type=int, default=YT_TARGET_HEIGHT, help=f"Target height (default: {YT_TARGET_HEIGHT}).")
    parser.add_argument("--format", type=str, default="JPEG", choices=["JPEG", "PNG"], help="Output format (default: JPEG).")
    parser.add_argument("--quality", type=int, default=92, help="JPEG quality (1-95; default: 92).")
    parser.add_argument("--canvas_bg_color", type=str, default="white", help="Canvas background color (default: white).")
    parser.add_argument("--diff_bg_strategy", type=str, default="white", choices=["top_left_pixel", "white", "black"], help="Strategy for BBox diff background (default: white).")
    parser.add_argument("--threshold", type=int, default=50, help="Threshold for binary mask (0-255; default: 50, advisor suggests 180-220 for JPEGs).") # Updated default and comment
    parser.add_argument("--safety_border", type=int, default=0, help="Safety border ON MASK edges for BBox detection (default: 0 for this version, per advisor test).") # Default to 0 for this test
    parser.add_argument("--bbox_padding", type=int, default=20, help="Padding (px) to add around the tight BBox (default: 20).")
    parser.add_argument("--skip_all_bg", action='store_true', help="Skip images if no content found after masking.")
    parser.add_argument("--resample_filter", type=str, default="lanczos", choices=["nearest", "lanczos", "bicubic", "bilinear", "box", "hamming"], help="Resampling filter (default: lanczos).")
    parser.add_argument("--debug_save_intermediate", action='store_true', help="Save debug mask and pre-resize cropped images.")
    parser.add_argument("--apply_dilation", action='store_true', help="Apply dilation to mask.")
    parser.add_argument("--min_crop_width_ratio", type=float, default=None, help="Min crop width as ratio of ORIGINAL image width (e.g., 0.4). Set to 0 or omit to disable.")
    parser.add_argument("--max_workers", type=int, default=None, help="Max worker processes (default: CPU count).")
    args = parser.parse_args()

    input_path, output_path = Path(args.input_dir), Path(args.output_dir)
    if not input_path.is_dir(): logger.error(f"Input dir not found: {input_path}"); return
    output_path.mkdir(parents=True, exist_ok=True)

    image_files = [f for p in args.glob_pattern.split(',') for f in input_path.glob(p.strip())]
    if not image_files: logger.warning(f"No images in '{input_path}' matching '{args.glob_pattern}'"); return

    logger.info(f"Found {len(image_files)} images. Output: {output_path}.")
    logger.info(f"Settings: Thr={args.threshold}, SafeBorderOnMask={args.safety_border}, BBoxPad={args.bbox_padding}, DiffStrat={args.diff_bg_strategy}, Dil={args.apply_dilation}")
    if args.min_crop_width_ratio is not None and args.min_crop_width_ratio > 0: logger.info(f"MinCropWRatio: {args.min_crop_width_ratio}")
    else: logger.info("MinCropWRatio: Disabled")


    processed_count, skipped_or_failed_count = 0, 0
    with ProcessPoolExecutor(max_workers=args.max_workers) as executor:
        tasks = [(file, output_path, args.width, args.height, args.format, args.quality,
                  args.canvas_bg_color, args.diff_bg_strategy, args.threshold,
                  args.skip_all_bg, args.resample_filter, args.debug_save_intermediate,
                  args.apply_dilation, args.min_crop_width_ratio, args.safety_border,
                  args.bbox_padding # New arg
                 ) for file in image_files]
        for result in executor.map(worker_function_unpacker, tasks):
            if result: processed_count += 1
            else: skipped_or_failed_count += 1
    logger.info(f"Batch complete. Processed: {processed_count}, Skipped/Failed: {skipped_or_failed_count}")

if __name__ == "__main__":
    main()
