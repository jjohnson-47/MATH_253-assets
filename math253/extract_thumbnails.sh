#!/bin/bash
# Full FFmpeg Thumbnail Extractor
# Works with complete ffmpeg installation

extract_thumbnail() {
    local input_file="$1"
    local output_dir="$2"
    
    local basename=$(basename "$input_file")
    basename="${basename%.*}"
    local output_file="${output_dir}/${basename}_thumbnail.jpg"
    
    echo "Processing: $input_file"
    
    # Extract first frame with proper flags for single image output
    ffmpeg -i "$input_file" \
        -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black" \
        -frames:v 1 \
        -update 1 \
        -q:v 2 \
        "$output_file" \
        -y 2>/dev/null
    
    if [ $? -eq 0 ] && [ -f "$output_file" ]; then
        # Get file size for verification
        local size=$(du -h "$output_file" | cut -f1)
        echo "✓ Thumbnail saved: $output_file ($size)"
        return 0
    else
        echo "✗ Failed to process: $input_file"
        return 1
    fi
}

main() {
    local input_dir="${1:-.}"
    local output_dir="${2:-./thumbnails}"
    
    mkdir -p "$output_dir"
    
    echo "FFmpeg Thumbnail Extractor"
    echo "=========================="
    echo "Input directory: $input_dir"
    echo "Output directory: $output_dir"
    echo "Target size: 1280x720 (YouTube optimal)"
    echo ""
    
    local count=0
    local success=0
    local total_size=0
    
    cd "$input_dir" || exit 1
    
    # Process all MP4 files
    for file in *.mp4 *.MP4; do
        if [ -f "$file" ]; then
            ((count++))
            if extract_thumbnail "$file" "$output_dir"; then
                ((success++))
            fi
        fi
    done
    
    echo ""
    echo "Results:"
    echo "========"
    if [ $count -eq 0 ]; then
        echo "No MP4 files found in $input_dir"
    else
        echo "Files processed: $count"
        echo "Successful: $success"
        echo "Failed: $((count - success))"
        
        if [ $success -gt 0 ]; then
            echo ""
            echo "Thumbnails saved in: $output_dir"
            echo "Files created:"
            ls -la "$output_dir"/*.jpg 2>/dev/null | awk '{print "  " $9 " (" $5 " bytes)"}'
        fi
    fi
}

# Show usage
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "FFmpeg Thumbnail Extractor"
    echo "=========================="
    echo ""
    echo "Usage: $0 [input_directory] [output_directory]"
    echo ""
    echo "Examples:"
    echo "  $0                           # Process current directory → ./thumbnails"
    echo "  $0 . ./thumbnails           # Same as above (explicit)"
    echo "  $0 /path/to/videos          # Process videos → /path/to/videos/thumbnails"
    echo "  $0 /path/to/videos /tmp/thumbs # Custom input and output directories"
    echo ""
    echo "Features:"
    echo "  • Extracts first frame from MP4 files"
    echo "  • Resizes to 1280x720 (YouTube optimal)"
    echo "  • Maintains aspect ratio with black padding"
    echo "  • High quality JPEG output (q=2)"
    echo "  • Handles spaces and special characters in filenames"
    echo ""
    exit 0
fi

main "$@"
