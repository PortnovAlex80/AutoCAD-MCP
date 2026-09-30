;;; mcp_dispatch.lsp - File-based IPC dispatcher for AutoCAD MCP v4.0.0
;;;
;;; Protocol:
;;;   1. Python writes command JSON to C:/temp/autocad_mcp_cmd_{id}.json
;;;   2. Python types "(c:mcp-dispatch)" + Enter
;;;   3. This function reads cmd, dispatches via command map, writes result JSON
;;;   4. Python polls for C:/temp/autocad_mcp_result_{id}.json
;;;
;;; SECURITY: No raw eval — dispatcher uses a command whitelist/map.
;;; Compatible with AutoCAD LT 2024+.

;; Load dependencies
(if (not report-error)
  (defun report-error (msg) (princ (strcat "\nERROR: " msg)))
)

;; IPC directory
(setq *mcp-ipc-dir* (getenv "AUTOCAD_MCP_IPC_DIR"))
(if (or (null *mcp-ipc-dir*) (= *mcp-ipc-dir* ""))
  (setq *mcp-ipc-dir* "C:/temp")
)
(setq *mcp-ipc-dir* (vl-string-translate "\\" "/" *mcp-ipc-dir*))
(if (/= (substr *mcp-ipc-dir* (strlen *mcp-ipc-dir*) 1) "/")
  (setq *mcp-ipc-dir* (strcat *mcp-ipc-dir* "/"))
)

;; -----------------------------------------------------------------------
;; JSON-like output helpers (minimal, no external library)
;; -----------------------------------------------------------------------

(defun mcp-write-result (filepath request-id ok-flag payload error-msg / fp)
  "Write a result JSON file. Atomic: write to .tmp then rename."
  (setq tmp-path (strcat filepath ".tmp"))
  (setq fp (open tmp-path "w"))
  (if fp
    (progn
      (write-line "{" fp)
      (write-line (strcat "  \"request_id\": \"" request-id "\",") fp)
      (if ok-flag
        (progn
          (write-line "  \"ok\": true," fp)
          (write-line (strcat "  \"payload\": " payload) fp)
        )
        (progn
          (write-line "  \"ok\": false," fp)
          (write-line (strcat "  \"error\": \"" (mcp-escape-string error-msg) "\"") fp)
        )
      )
      (write-line "}" fp)
      (close fp)
      ;; Rename .tmp to final path (atomic on NTFS)
      (vl-file-rename tmp-path filepath)
    )
    (princ (strcat "\nMCP: Cannot open result file: " tmp-path))
  )
)

(defun mcp-escape-string (s / result i ch)
  "Escape quotes and backslashes in a string for JSON."
  (if (null s) (setq s ""))
  (setq result "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (cond
      ((= ch "\"") (setq result (strcat result "\\\"")))
      ((= ch "\\") (setq result (strcat result "\\\\")))
      (t (setq result (strcat result ch)))
    )
    (setq i (1+ i))
  )
  result
)

(defun mcp-read-file-lines (filepath / fp line lines)
  "Read all lines from a file into a single string."
  (setq fp (open filepath "r"))
  (if (not fp) (progn (princ (strcat "\nMCP: Cannot read: " filepath)) nil)
    (progn
      (setq lines "")
      (while (setq line (read-line fp))
        (setq lines (strcat lines line))
      )
      (close fp)
      lines
    )
  )
)

;; -----------------------------------------------------------------------
;; Simple JSON parser (extracts string values by key)
;; -----------------------------------------------------------------------

(defun mcp-json-unescape-string (s / result i ch next)
  "Decode the JSON escapes used by the Python IPC writer."
  (setq result "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (if (and (= ch "\\") (< i (strlen s)))
      (progn
        (setq next (substr s (1+ i) 1))
        (cond
          ((= next "\\") (setq result (strcat result "\\")))
          ((= next "\"") (setq result (strcat result "\"")))
          ((= next "/") (setq result (strcat result "/")))
          ((= next "n") (setq result (strcat result (chr 10))))
          ((= next "r") (setq result (strcat result (chr 13))))
          ((= next "t") (setq result (strcat result (chr 9))))
          (t (setq result (strcat result "\\" next)))
        )
        (setq i (+ i 2))
      )
      (progn
        (setq result (strcat result ch))
        (setq i (1+ i))
      )
    )
  )
  result
)

(defun mcp-json-get-string (json key / search-str pos end-pos value)
  "Extract a string value for a given key from JSON text."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      ;; Find the colon after key
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          ;; Find opening quote of value
          (setq pos (vl-string-search "\"" json (1+ pos)))
          (if (null pos) nil
            (progn
              (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after quote
              ;; Find closing quote (skip escaped quotes)
              (setq end-pos pos)
              (while (and (<= end-pos (strlen json))
                          (or (= end-pos pos)
                              (/= (substr json end-pos 1) "\"")))
                ;; Handle escaped characters
                (if (= (substr json end-pos 1) "\\")
                  (setq end-pos (+ end-pos 2))
                  (setq end-pos (1+ end-pos))
                )
              )
              (mcp-json-unescape-string (substr json pos (- end-pos pos)))
            )
          )
        )
      )
    )
  )
)

(defun mcp-json-get-number (json key / search-str pos num-start num-end ch)
  "Extract a number value for a given key from JSON text."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after colon
          ;; Skip whitespace
          (while (and (<= pos (strlen json))
                      (member (substr json pos 1) '(" " "\t" "\n")))
            (setq pos (1+ pos))
          )
          ;; Read number
          (setq num-start pos num-end pos)
          (while (and (<= num-end (strlen json))
                      (or (member (substr json num-end 1) '("0" "1" "2" "3" "4" "5" "6" "7" "8" "9" "." "-" "+"))
                      ))
            (setq num-end (1+ num-end))
          )
          (atof (substr json num-start (- num-end num-start)))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; String splitting utility (used by semicolon-delimited encodings)
;; -----------------------------------------------------------------------

(defun mcp-split-string (str delim / pos result token)
  "Split a string by single-char delimiter. Returns a list of strings."
  (setq result '())
  (while (setq pos (vl-string-search delim str))
    (setq token (substr str 1 pos))
    (setq result (append result (list token)))
    (setq str (substr str (+ pos 2)))
  )
  (setq result (append result (list str)))
  result
)

(defun mcp-selection-from-handles (encoded / selection handle ent)
  "Build a selection set from a semicolon-delimited handle list."
  (setq selection (ssadd))
  (if encoded
    (foreach handle (mcp-split-string encoded ";")
      (setq ent (handent handle))
      (if ent (ssadd ent selection))
    )
  )
  (if (> (sslength selection) 0) selection nil)
)

(defun mcp-parse-picked-target (encoded / at-pos handle coords comma-pos x y ent)
  "Parse handle@x,y into an entity-pickpoint pair accepted by command."
  (setq at-pos (vl-string-search "@" encoded))
  (if at-pos
    (progn
      (setq handle (substr encoded 1 at-pos))
      (setq coords (substr encoded (+ at-pos 2)))
      (setq comma-pos (vl-string-search "," coords))
      (if comma-pos
        (progn
          (setq x (atof (substr coords 1 comma-pos)))
          (setq y (atof (substr coords (+ comma-pos 2))))
          (setq ent (handent handle))
          (if ent (list ent (list x y 0)) nil)
        )
        nil
      )
    )
    nil
  )
)

;; -----------------------------------------------------------------------
;; Command dispatcher — WHITELIST ONLY, no eval
;; -----------------------------------------------------------------------

(defun mcp-dispatch-command (cmd-name params-json / result)
  "Dispatch a command by name. Returns (ok . payload-or-error)."
  (cond
    ;; --- Ping ---
    ((= cmd-name "ping")
 (cons T "{\"pong\":true,\"dispatcher_version\":\"4.0.0\"}"))

    ;; --- Freehand LISP execution ---
    ((= cmd-name "execute-lisp")
     (mcp-cmd-execute-lisp params-json))

    ;; --- Undo / Redo ---
    ((= cmd-name "undo")
     (command "_.UNDO" "1") (cons T "\"undone\""))

    ((= cmd-name "redo")
     (command "_.REDO") (cons T "\"redone\""))

    ((= cmd-name "transaction-begin")
     (command "_.UNDO" "_BEGIN") (cons T "{\"transaction\":\"open\"}"))

    ((= cmd-name "transaction-commit")
     (command "_.UNDO" "_END") (cons T "{\"transaction\":\"committed\"}"))

    ((= cmd-name "transaction-rollback")
     (command "_.UNDO" "_END")
     (command "_.UNDO" "1")
     (cons T "{\"transaction\":\"rolled_back\"}"))

    ;; --- Drawing info ---
    ((= cmd-name "drawing-info")
     (mcp-cmd-drawing-info))

    ((= cmd-name "drawing-setup-mechanical")
     (mcp-cmd-drawing-setup-mechanical))

    ;; --- Layer operations ---
    ((= cmd-name "layer-list")
     (mcp-cmd-layer-list))

    ((= cmd-name "layer-create")
     (mcp-cmd-layer-create params-json))

    ((= cmd-name "layer-set-current")
     (mcp-cmd-layer-set-current params-json))

    ((= cmd-name "layer-set-properties")
     (mcp-cmd-layer-set-properties params-json))

    ((= cmd-name "layer-freeze")
     (mcp-cmd-layer-freeze params-json))

    ((= cmd-name "layer-thaw")
     (mcp-cmd-layer-thaw params-json))

    ((= cmd-name "layer-lock")
     (mcp-cmd-layer-lock params-json))

    ((= cmd-name "layer-unlock")
     (mcp-cmd-layer-unlock params-json))

    ;; --- Entity creation ---
    ((= cmd-name "create-line")
     (mcp-cmd-create-line params-json))

    ((= cmd-name "create-circle")
     (mcp-cmd-create-circle params-json))

    ((= cmd-name "create-polyline")
     (mcp-cmd-create-polyline params-json))

    ((= cmd-name "create-rectangle")
     (mcp-cmd-create-rectangle params-json))

    ((= cmd-name "create-text")
     (mcp-cmd-create-text params-json))

    ((= cmd-name "create-arc")
     (mcp-cmd-create-arc params-json))

    ((= cmd-name "create-ellipse")
     (mcp-cmd-create-ellipse params-json))

    ((= cmd-name "create-mtext")
     (mcp-cmd-create-mtext params-json))

    ((= cmd-name "create-hatch")
     (mcp-cmd-create-hatch params-json))

    ;; --- Entity queries ---
    ((= cmd-name "entity-count")
     (mcp-cmd-entity-count params-json))

    ((= cmd-name "entity-list")
     (mcp-cmd-entity-list params-json))

    ((= cmd-name "entity-get")
     (mcp-cmd-entity-get params-json))

    ((= cmd-name "entity-erase")
     (mcp-cmd-entity-erase params-json))

    ;; --- Entity modification ---
    ((= cmd-name "entity-move")
     (mcp-cmd-entity-move params-json))

    ((= cmd-name "entity-copy")
     (mcp-cmd-entity-copy params-json))

    ((= cmd-name "entity-rotate")
     (mcp-cmd-entity-rotate params-json))

    ((= cmd-name "entity-scale")
     (mcp-cmd-entity-scale params-json))

    ((= cmd-name "entity-mirror")
     (mcp-cmd-entity-mirror params-json))

    ((= cmd-name "entity-offset")
     (mcp-cmd-entity-offset params-json))

    ((= cmd-name "entity-array")
     (mcp-cmd-entity-array params-json))

    ((= cmd-name "entity-fillet")
     (mcp-cmd-entity-fillet params-json))

    ((= cmd-name "entity-chamfer")
     (mcp-cmd-entity-chamfer params-json))

    ((= cmd-name "entity-trim")
     (mcp-cmd-entity-trim params-json))

    ((= cmd-name "entity-extend")
     (mcp-cmd-entity-extend params-json))

    ((= cmd-name "entity-break")
     (mcp-cmd-entity-break params-json))

    ((= cmd-name "entity-join")
     (mcp-cmd-entity-join params-json))

    ((= cmd-name "entity-constrain")
     (mcp-cmd-entity-constrain params-json))

    ;; --- View ---
    ((= cmd-name "zoom-extents")
     (command "_.ZOOM" "_E")
     (cons T "\"zoomed to extents\""))

    ((= cmd-name "zoom-window")
     (progn
       (setq x1 (mcp-json-get-number params-json "x1"))
       (setq y1 (mcp-json-get-number params-json "y1"))
       (setq x2 (mcp-json-get-number params-json "x2"))
       (setq y2 (mcp-json-get-number params-json "y2"))
       (command "_.ZOOM" "_W" (list x1 y1 0) (list x2 y2 0))
       (cons T "\"zoomed to window\"")))

    ;; --- Drawing file ops ---
    ((= cmd-name "drawing-save")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if (and path (> (strlen path) 0))
         (progn
           (setvar "FILEDIA" 0)
           (command "_.SAVEAS" "" path)
           (setvar "FILEDIA" 1)
           (cons T (strcat "\"saved to: " (mcp-escape-string path) "\"")))
         (progn (command "_.QSAVE") (cons T "\"saved\"")))))

    ((= cmd-name "drawing-save-as-dxf")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (progn (command "_.SAVEAS" "DXF" path) (cons T (strcat "\"" path "\"")))
         (cons nil "Save path required"))))

    ((= cmd-name "drawing-purge")
     (command "_.-PURGE" "_ALL" "*" "_N")
     (cons T "\"purged\""))

    ((= cmd-name "drawing-open")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (progn
           (setvar "FILEDIA" 0)
           (command "_.OPEN" path)
           (setvar "FILEDIA" 1)
           (cons T (strcat "\"opened: " (mcp-escape-string path) "\"")))
         (cons nil "Path required"))))

    ;; --- P&ID ---
    ((= cmd-name "pid-setup-layers")
     (if c:setup-pid-layers
       (progn (c:setup-pid-layers) (cons T "\"P&ID layers created\""))
       (cons nil "pid_tools.lsp not loaded")))

    ((= cmd-name "pid-insert-symbol")
     (mcp-cmd-pid-insert-symbol params-json))

    ((= cmd-name "pid-draw-process-line")
     (mcp-cmd-pid-draw-process-line params-json))

    ((= cmd-name "pid-connect-equipment")
     (mcp-cmd-pid-connect-equipment params-json))

    ((= cmd-name "pid-add-flow-arrow")
     (mcp-cmd-pid-add-flow-arrow params-json))

    ((= cmd-name "pid-add-equipment-tag")
     (mcp-cmd-pid-add-equipment-tag params-json))

    ((= cmd-name "pid-add-line-number")
     (mcp-cmd-pid-add-line-number params-json))

    ((= cmd-name "pid-insert-valve")
     (mcp-cmd-pid-insert-valve params-json))

    ((= cmd-name "pid-insert-instrument")
     (mcp-cmd-pid-insert-instrument params-json))

    ((= cmd-name "pid-insert-pump")
     (mcp-cmd-pid-insert-pump params-json))

    ((= cmd-name "pid-insert-tank")
     (mcp-cmd-pid-insert-tank params-json))

    ;; --- Block operations ---
    ((= cmd-name "block-list")
     (mcp-cmd-block-list))

    ((= cmd-name "block-insert")
     (mcp-cmd-block-insert params-json))

    ((= cmd-name "block-insert-with-attributes")
     (mcp-cmd-block-insert-with-attribs params-json))

    ((= cmd-name "block-get-attributes")
     (mcp-cmd-block-get-attributes params-json))

    ((= cmd-name "block-update-attribute")
     (mcp-cmd-block-update-attribute params-json))

    ((= cmd-name "block-define")
     (cons nil "block-define not available via IPC (use ezdxf backend)"))

    ;; --- Annotation ---
    ((= cmd-name "create-dimension-linear")
     (mcp-cmd-create-dimension-linear params-json))

    ((= cmd-name "create-dimension-aligned")
     (mcp-cmd-create-dimension-aligned params-json))

    ((= cmd-name "create-dimension-angular")
     (mcp-cmd-create-dimension-angular params-json))

    ((= cmd-name "create-dimension-radius")
     (mcp-cmd-create-dimension-radius params-json))

    ((= cmd-name "create-leader")
     (mcp-cmd-create-leader params-json))

    ;; --- Drawing management ---
    ((= cmd-name "drawing-create")
     (mcp-cmd-drawing-create params-json))

    ((= cmd-name "drawing-get-variables")
     (mcp-cmd-drawing-get-variables params-json))

    ((= cmd-name "drawing-plot-pdf")
     (mcp-cmd-drawing-plot-pdf params-json))

    ;; --- P&ID list symbols ---
    ((= cmd-name "pid-list-symbols")
     (mcp-cmd-pid-list-symbols params-json))

    ;; --- Inquiry ---
    ((= cmd-name "measure-length")
     (mcp-cmd-measure-length params-json))

    ((= cmd-name "measure-area")
     (mcp-cmd-measure-area params-json))

    ((= cmd-name "bbox")
     (mcp-cmd-bbox params-json))

    ((= cmd-name "summary")
     (mcp-cmd-summary))

    ;; --- Selection / spline / explode ---
    ((= cmd-name "entity-select")
     (mcp-cmd-entity-select params-json))

    ((= cmd-name "create-spline")
     (mcp-cmd-create-spline params-json))

    ((= cmd-name "entity-explode")
     (mcp-cmd-entity-explode params-json))

    ;; --- Styles ---
    ((= cmd-name "textstyle-list")
     (mcp-cmd-textstyle-list))

    ((= cmd-name "textstyle-create")
     (mcp-cmd-textstyle-create params-json))

    ((= cmd-name "textstyle-set-current")
     (mcp-cmd-textstyle-set-current params-json))

    ((= cmd-name "dimstyle-list")
     (mcp-cmd-dimstyle-list))

    ((= cmd-name "dimstyle-create")
     (mcp-cmd-dimstyle-create params-json))

    ((= cmd-name "dimstyle-set-current")
     (mcp-cmd-dimstyle-set-current params-json))

    ((= cmd-name "linetype-list")
     (mcp-cmd-linetype-list))

    ;; --- Layouts ---
    ((= cmd-name "layout-list")
     (mcp-cmd-layout-list))

    ((= cmd-name "layout-create")
     (mcp-cmd-layout-create params-json))

    ((= cmd-name "layout-set-current")
     (mcp-cmd-layout-set-current params-json))

    ((= cmd-name "layout-add-viewport")
     (mcp-cmd-layout-add-viewport params-json))

    ;; --- Tables ---
    ((= cmd-name "table-create")
     (mcp-cmd-table-create params-json))

    ((= cmd-name "table-set-cell")
     (mcp-cmd-table-set-cell params-json))

    ((= cmd-name "table-set-col-widths")
     (mcp-cmd-table-set-col-widths params-json))

    ((= cmd-name "table-set-row-heights")
     (mcp-cmd-table-set-row-heights params-json))

    ;; --- External references ---
    ((= cmd-name "xref-list")
     (mcp-cmd-xref-list))

    ((= cmd-name "xref-attach")
     (mcp-cmd-xref-attach params-json))

    ((= cmd-name "xref-detach")
     (mcp-cmd-xref-detach params-json))

    ((= cmd-name "xref-reload")
     (mcp-cmd-xref-reload params-json))

    ;; --- Unknown ---
    (t (cons nil (strcat "Unknown command: " cmd-name)))
  )
)

;; -----------------------------------------------------------------------
;; Command implementations
;; -----------------------------------------------------------------------

(defun mcp-cmd-drawing-info ( / count layers layer-list)
  "Return drawing info: entity count, layers, extents."
  (setq count 0)
  (setq ent (entnext))
  (while ent
    (setq count (1+ count))
    (setq ent (entnext ent))
  )
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",\"" (cdr (assoc 2 layers)) "\""))
      (setq layer-list (strcat "\"" (cdr (assoc 2 layers)) "\""))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"entity_count\":" (itoa count) ",\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-layer-list ( / layers layer-list name)
  "Return all layers as JSON array."
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (setq name (cdr (assoc 2 layers)))
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
      (setq layer-list (strcat "{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-drawing-setup-mechanical ( / names)
  "Create the standard GB/T monochrome drafting layers without prompts."
  (mcp-upsert-layer "OUTLINE" "7" "CONTINUOUS" "0.50")
  (mcp-upsert-layer "THIN" "7" "CONTINUOUS" "0.20")
  (mcp-upsert-layer "CENTER" "7" "CENTER" "0.20")
  (mcp-upsert-layer "HIDDEN" "7" "HIDDEN" "0.20")
  (mcp-upsert-layer "HATCH" "7" "CONTINUOUS" "0.20")
  (mcp-upsert-layer "DIM" "7" "CONTINUOUS" "0.20")
  (mcp-upsert-layer "TEXT" "7" "CONTINUOUS" "0.20")
  (cons T "{\"profile\":\"mechanical-gbt\",\"layers\":[\"OUTLINE\",\"THIN\",\"CENTER\",\"HIDDEN\",\"HATCH\",\"DIM\",\"TEXT\"]}")
)

(defun mcp-cmd-layer-create (params / name color linetype lineweight actual-linetype)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (setq lineweight (mcp-json-get-string params "lineweight"))
  (if (not color) (setq color "white"))
  (if (not linetype) (setq linetype "CONTINUOUS"))
  (if (not name)
    (cons nil "Layer name required")
    (progn
      (setq actual-linetype (mcp-upsert-layer name color linetype lineweight))
      (cons T (strcat
        "{\"name\":\"" (mcp-escape-string name)
        "\",\"linetype\":\"" actual-linetype "\"}"
      ))
    )
  )
)

(defun mcp-cmd-layer-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (setvar "CLAYER" name)
  (cons T (strcat "{\"current_layer\":\"" name "\"}"))
)

(defun mcp-cmd-create-line (params / x1 y1 x2 y2 layer)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_LINE" (list x1 y1 0.0) (list x2 y2 0.0) "")
  (cons T (strcat "{\"entity_type\":\"LINE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-circle (params / cx cy radius layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_CIRCLE" (list cx cy 0.0) radius)
  (cons T (strcat "{\"entity_type\":\"CIRCLE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-polyline (params / pts-str closed layer pairs pt-str cx cy)
  (setq pts-str (mcp-json-get-string params "points_str"))
  (setq closed (mcp-json-get-string params "closed"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_PLINE")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (setq cx (atof (car (mcp-split-string pt-str ","))))
        (setq cy (atof (cadr (mcp-split-string pt-str ","))))
        (command (list cx cy 0.0))
      )
      (if (= closed "1") (command "_C") (command ""))
      (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\""
                      (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
  )
)

(defun mcp-cmd-create-rectangle (params / x1 y1 x2 y2 layer before-ent created-ent)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (setq before-ent (entlast))
  (command "_RECTANG" (list x1 y1 0.0) (list x2 y2 0.0))
  (setq created-ent (entlast))
  (if (or (not created-ent) (eq before-ent created-ent))
    (cons nil "RECTANG completed without creating an entity")
    (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\"" (cdr (assoc 5 (entget created-ent))) "\"}"))
  )
)

(defun mcp-cmd-create-text (params / x y text height rotation layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not height) (setq height 2.5))
  (if (not rotation) (setq rotation 0.0))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_TEXT" "J" "M" (list x y 0.0) height rotation text)
  (cons T (strcat "{\"entity_type\":\"TEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-entity-count (params / layer count ent ent-data)
  (setq layer (mcp-json-get-string params "layer"))
  (setq count 0 ent (entnext))
  (while ent
    (setq ent-data (entget ent))
    (if (or (not layer) (= (cdr (assoc 8 ent-data)) layer))
      (setq count (1+ count))
    )
    (setq ent (entnext ent))
  )
  (cons T (strcat "{\"count\":" (itoa count) "}"))
)

(defun mcp-cmd-entity-list (params / layer entities ent ent-data etype handle elayer)
  (setq layer (mcp-json-get-string params "layer"))
  (setq entities "" ent (entnext))
  (while ent
    (setq ent-data (entget ent))
    (setq etype (cdr (assoc 0 ent-data)))
    (setq handle (cdr (assoc 5 ent-data)))
    (setq elayer (cdr (assoc 8 ent-data)))
    (if (or (not layer) (= elayer layer))
      (progn
        (if (> (strlen entities) 0)
          (setq entities (strcat entities ","))
        )
        (setq entities (strcat entities "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\"}"))
      )
    )
    (setq ent (entnext ent))
  )
  (cons T (strcat "{\"entities\":[" entities "]}"))
)

(defun mcp-cmd-entity-erase (params / entity-id ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (progn
      (setq ent (entlast))
      (if ent (progn (entdel ent) (cons T "\"erased last entity\""))
        (cons nil "No entity to erase")))
    (progn
      (setq ent (handent entity-id))
      (if ent (progn (entdel ent) (cons T (strcat "\"erased " entity-id "\"")))
        (cons nil (strcat "Entity not found: " entity-id))))
  )
)

(defun mcp-cmd-entity-move (params / entity-id dx dy ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (command "_.MOVE" ent "" '(0 0 0) (list dx dy 0))
      (cons T "\"moved\""))
    (cons nil "Entity not found")
  )
)

;; --- Freehand LISP execution ---

(defun mcp-cmd-execute-lisp (params / code-file result old-secureload)
  (setq code-file (mcp-json-get-string params "code_file"))
  (if (not code-file)
    (cons nil "code_file parameter required")
    (if (not (findfile code-file))
      (cons nil (strcat "Code file not found: " code-file))
      (progn
        ;; Suppress SECURELOAD dialog for MCP temp files
        (setq old-secureload (getvar "SECURELOAD"))
        (setvar "SECURELOAD" 0)
        (setq result (vl-catch-all-apply 'load (list code-file)))
        (setvar "SECURELOAD" old-secureload)
        (if (vl-catch-all-error-p result)
          (cons nil (strcat "LISP error: " (vl-catch-all-error-message result)))
          (cons T (strcat "\"" (mcp-escape-string (vl-princ-to-string result)) "\""))
        )
      )
    )
  )
)

;; --- Drawing create implementation ---

(defun mcp-cmd-drawing-create (params / ss)
  "Reset current drawing to a clean state (erase all, purge, reset to layer 0).
   Using _.NEW would create a new document tab with a fresh LISP namespace,
   breaking the IPC dispatcher. This approach preserves the dispatcher."
  (if (setq ss (ssget "_X"))
    (progn (command "_.ERASE" ss "") (setq ss nil))
  )
  (setvar "CLAYER" "0")
  (command "_.-PURGE" "_ALL" "*" "_N")
  (cons T (strcat "{\"drawing\":\"" (mcp-escape-string (getvar "DWGNAME")) "\"}"))
)

;; --- P&ID command implementations ---

(defun mcp-cmd-pid-insert-symbol (params / category symbol x y scale rotation)
  (setq category (mcp-json-get-string params "category"))
  (setq symbol (mcp-json-get-string params "symbol"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pid-block
    (progn
      (c:insert-pid-block category symbol x y scale rotation)
      (cons T (strcat "{\"symbol\":\"" symbol "\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-draw-process-line (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:draw-process-line
    (progn (c:draw-process-line x1 y1 x2 y2) (cons T "\"process line drawn\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-connect-equipment (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:connect-equipment
    (progn (c:connect-equipment x1 y1 x2 y2) (cons T "\"equipment connected\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-flow-arrow (params / x y rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:add-flow-arrow
    (progn (c:add-flow-arrow x y rotation) (cons T "\"flow arrow added\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-equipment-tag (params / x y tag description)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq description (mcp-json-get-string params "description"))
  (if (not description) (setq description ""))
  (if c:add-equipment-tag
    (progn (c:add-equipment-tag x y tag description) (cons T (strcat "\"tagged: " tag "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-line-number (params / x y line-num spec)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq line-num (mcp-json-get-string params "line_num"))
  (setq spec (mcp-json-get-string params "spec"))
  (if c:add-line-number
    (progn (c:add-line-number x y line-num spec) (cons T (strcat "\"line number: " line-num "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-valve (params / x y valve-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq valve-type (mcp-json-get-string params "valve_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-valve-on-line
    (progn (c:insert-valve-on-line x y valve-type rotation) (cons T (strcat "\"valve: " valve-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-instrument (params / x y inst-type rotation tag-id range-value)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq inst-type (mcp-json-get-string params "instrument_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq tag-id (mcp-json-get-string params "tag_id"))
  (setq range-value (mcp-json-get-string params "range_value"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-instrument
    (progn
      (c:insert-instrument x y inst-type rotation)
      (if (and tag-id (> (strlen tag-id) 0))
        (c:insert-instrument-with-tag x y inst-type tag-id (if range-value range-value ""))
      )
      (cons T (strcat "\"instrument: " inst-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-pump (params / x y pump-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq pump-type (mcp-json-get-string params "pump_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pump
    (progn (c:insert-pump x y pump-type rotation) (cons T (strcat "\"pump: " pump-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-tank (params / x y tank-type scale)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tank-type (mcp-json-get-string params "tank_type"))
  (setq scale (mcp-json-get-number params "scale"))
  (if (not scale) (setq scale 1.0))
  (if c:insert-tank
    (progn (c:insert-tank x y tank-type scale) (cons T (strcat "\"tank: " tank-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

;; --- Additional entity creation ---

(defun mcp-cmd-create-arc (params / cx cy radius sa ea layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq sa (mcp-json-get-number params "start_angle"))
  (setq ea (mcp-json-get-number params "end_angle"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ARC" "_C" (list cx cy 0.0) (list (+ cx radius) cy 0.0) "_A" (- ea sa))
  (cons T (strcat "{\"entity_type\":\"ARC\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-ellipse (params / cx cy mx my ratio layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq mx (mcp-json-get-number params "major_x"))
  (setq my (mcp-json-get-number params "major_y"))
  (setq ratio (mcp-json-get-number params "ratio"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ELLIPSE" "_C" (list cx cy 0.0) (list mx my 0.0) ratio)
  (cons T (strcat "{\"entity_type\":\"ELLIPSE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-mtext (params / x y width text height layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq width (mcp-json-get-number params "width"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (if (not height) (setq height 2.5))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_MTEXT" (list x y 0.0) "_H" height "_W" width text "")
  (cons T (strcat "{\"entity_type\":\"MTEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-hatch (params / entity-id pattern angle scale layer ent old-hpang old-hpscale old-layer result before-ent hatch-ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq pattern (mcp-json-get-string params "pattern"))
  (setq angle (mcp-json-get-number params "angle"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq layer (mcp-json-get-string params "layer"))
  (if (not pattern) (setq pattern "ANSI31"))
  (if (not angle) (setq angle 0.0))
  (if (not scale) (setq scale 1.0))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (setq old-hpang (getvar "HPANG"))
      (setq old-hpscale (getvar "HPSCALE"))
      (setq old-layer (getvar "CLAYER"))
      (if layer
        (progn
          (ensure_layer_exists layer "white" "CONTINUOUS")
          (setvar "CLAYER" layer)
        )
      )
      (setvar "HPANG" angle)
      (setvar "HPSCALE" scale)
      (setq before-ent (entlast))
      (setq result (vl-catch-all-apply 'mcp-run-hatch-command (list ent pattern scale angle)))
      (setvar "HPANG" old-hpang)
      (setvar "HPSCALE" old-hpscale)
      (setvar "CLAYER" old-layer)
      (setq hatch-ent (entlast))
      (cond
        ((vl-catch-all-error-p result)
          (cons nil (strcat "Hatch failed: " (vl-catch-all-error-message result))))
        ((or (not hatch-ent) (eq before-ent hatch-ent))
          (cons nil "HATCH completed without creating an entity"))
        (t
          (cons T (strcat
              "{\"entity_type\":\"HATCH\",\"handle\":\""
              (cdr (assoc 5 (entget hatch-ent)))
              "\",\"pattern\":\"" (mcp-escape-string pattern)
              "\",\"angle\":" (rtos angle 2 6)
              ",\"scale\":" (rtos scale 2 6) "}"
            ))
        )
      )
    )
    (cons nil "Entity not found for hatching")
  )
)

;; --- Entity query: get ---

(defun mcp-cmd-entity-get (params / entity-id ent ent-data etype handle elayer result item point points pattern angle scale)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if (not ent)
    (cons nil (strcat "Entity not found: " entity-id))
    (progn
      (setq ent-data (entget ent))
      (setq etype (cdr (assoc 0 ent-data)))
      (setq handle (cdr (assoc 5 ent-data)))
      (setq elayer (cdr (assoc 8 ent-data)))
      (setq result (strcat "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\""))
      ;; Add type-specific info
      (cond
        ((= etype "LINE")
         (setq result (strcat result
           ",\"start\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"end\":[" (rtos (car (cdr (assoc 11 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 11 ent-data))) 2 6) "]")))
        ((= etype "CIRCLE")
         (setq result (strcat result
           ",\"center\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"radius\":" (rtos (cdr (assoc 40 ent-data)) 2 6))))
        ((= etype "ARC")
         (setq result (strcat result
           ",\"center\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"radius\":" (rtos (cdr (assoc 40 ent-data)) 2 6)
           ",\"start_angle\":" (rtos (* 180.0 (/ (cdr (assoc 50 ent-data)) pi)) 2 6)
           ",\"end_angle\":" (rtos (* 180.0 (/ (cdr (assoc 51 ent-data)) pi)) 2 6))))
        ((= etype "LWPOLYLINE")
         (setq points "")
         (foreach item ent-data
           (if (= (car item) 10)
             (progn
               (setq point (cdr item))
               (if (> (strlen points) 0) (setq points (strcat points ",")))
               (setq points (strcat points "[" (rtos (car point) 2 6) "," (rtos (cadr point) 2 6) "]"))
             )
           )
         )
         (setq result (strcat result
            ",\"points\":[" points "]"
            ",\"closed\":" (if (and (assoc 70 ent-data) (= 1 (logand 1 (cdr (assoc 70 ent-data))))) "true" "false"))))
         ((= etype "HATCH")
          ;; DXF group codes 2/52/41 are the native pattern, angle and scale.
          ;; Returning them lets the Python boundary verify a hatch instead of
          ;; trusting entlast alone.
          (setq pattern (if (assoc 2 ent-data) (cdr (assoc 2 ent-data)) ""))
          (setq angle (if (assoc 52 ent-data) (cdr (assoc 52 ent-data)) 0.0))
          (setq scale (if (assoc 41 ent-data) (cdr (assoc 41 ent-data)) 1.0))
          (setq result (strcat result
            ",\"pattern\":\"" (mcp-escape-string pattern) "\""
            ",\"angle\":" (rtos (* 180.0 (/ angle pi)) 2 6)
            ",\"scale\":" (rtos scale 2 6))))
         ((or (= etype "TEXT") (= etype "MTEXT"))
         (setq result (strcat result
           ",\"insert\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"text\":\"" (mcp-escape-string (cdr (assoc 1 ent-data))) "\""
           ",\"height\":" (rtos (cdr (assoc 40 ent-data)) 2 6)
           ",\"rotation\":" (rtos (* 180.0 (/ (if (assoc 50 ent-data) (cdr (assoc 50 ent-data)) 0.0) pi)) 2 6))))
        ((= etype "INSERT")
         (setq result (strcat result
           ",\"name\":\"" (mcp-escape-string (cdr (assoc 2 ent-data))) "\""
           ",\"insert\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"xscale\":" (rtos (if (assoc 41 ent-data) (cdr (assoc 41 ent-data)) 1.0) 2 6)
           ",\"yscale\":" (rtos (if (assoc 42 ent-data) (cdr (assoc 42 ent-data)) 1.0) 2 6)
           ",\"rotation\":" (rtos (* 180.0 (/ (if (assoc 50 ent-data) (cdr (assoc 50 ent-data)) 0.0) pi)) 2 6))))
        ((= etype "DIMENSION")
         (setq result (strcat result
           ",\"dimtype\":" (itoa (cdr (assoc 70 ent-data)))
           ",\"text\":\"" (mcp-escape-string (if (assoc 1 ent-data) (cdr (assoc 1 ent-data)) "")) "\""
           (if (assoc 42 ent-data) (strcat ",\"measurement\":" (rtos (cdr (assoc 42 ent-data)) 2 6)) ""))))
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

;; --- Entity modification commands ---

(defun mcp-cmd-entity-copy (params / entity-id dx dy ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.COPY" ent "" '(0 0 0) (list dx dy 0))
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-rotate (params / entity-id cx cy angle ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq angle (mcp-json-get-number params "angle"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.ROTATE" ent "" (list cx cy 0) angle) (cons T "\"rotated\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-scale (params / entity-id cx cy factor ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq factor (mcp-json-get-number params "factor"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.SCALE" ent "" (list cx cy 0) factor) (cons T "\"scaled\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-mirror (params / entity-id x1 y1 x2 y2 ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.MIRROR" ent "" (list x1 y1 0) (list x2 y2 0) "_N")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-offset (params / entity-id distance ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq distance (mcp-json-get-number params "distance"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.OFFSET" distance ent (list 0 0 0) "")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-array (params / entity-id rows cols row-dist col-dist ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq rows (fix (mcp-json-get-number params "rows")))
  (setq cols (fix (mcp-json-get-number params "cols")))
  (setq row-dist (mcp-json-get-number params "row_dist"))
  (setq col-dist (mcp-json-get-number params "col_dist"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.ARRAY" ent "" "_R" rows cols row-dist col-dist)
      (cons T (strcat "{\"rows\":" (itoa rows) ",\"cols\":" (itoa cols) "}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-fillet (params / id1 id2 radius ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.FILLET" "_R" radius)
      (command "_.FILLET" ent1 ent2)
      (cons T "\"filleted\""))
    (cons nil "One or both entities not found")
  )
)

(defun mcp-cmd-entity-chamfer (params / id1 id2 dist1 dist2 ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq dist1 (mcp-json-get-number params "dist1"))
  (setq dist2 (mcp-json-get-number params "dist2"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.CHAMFER" "_D" dist1 dist2)
      (command "_.CHAMFER" ent1 ent2)
      (cons T "\"chamfered\""))
    (cons nil "One or both entities not found")
  )
)

(defun mcp-cmd-entity-trim (params / cutters-str targets-str cutters token target count)
  (setq cutters-str (mcp-json-get-string params "cutters_str"))
  (setq targets-str (mcp-json-get-string params "targets_str"))
  (setq cutters (mcp-selection-from-handles cutters-str))
  (setq count 0)
  (if (and cutters targets-str)
    (progn
      (command "_.TRIM" cutters "")
      (foreach token (mcp-split-string targets-str ";")
        (setq target (mcp-parse-picked-target token))
        (if target (progn (command target) (setq count (1+ count))))
      )
      (command "")
      (cons T (strcat "{\"trimmed\":" (itoa count) "}"))
    )
    (cons nil "TRIM requires valid cutters and picked targets")
  )
)

(defun mcp-cmd-entity-extend (params / boundaries-str targets-str boundaries token target count)
  (setq boundaries-str (mcp-json-get-string params "boundaries_str"))
  (setq targets-str (mcp-json-get-string params "targets_str"))
  (setq boundaries (mcp-selection-from-handles boundaries-str))
  (setq count 0)
  (if (and boundaries targets-str)
    (progn
      (command "_.EXTEND" boundaries "")
      (foreach token (mcp-split-string targets-str ";")
        (setq target (mcp-parse-picked-target token))
        (if target (progn (command target) (setq count (1+ count))))
      )
      (command "")
      (cons T (strcat "{\"extended\":" (itoa count) "}"))
    )
    (cons nil "EXTEND requires valid boundaries and picked targets")
  )
)

(defun mcp-cmd-entity-break (params / entity-id ent x1 y1 x2 y2)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq ent (handent entity-id))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if ent
    (progn
      (command "_.BREAK" ent "_F" (list x1 y1 0) (list x2 y2 0))
      (cons T "{\"broken\":true}"))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-join (params / ids-str tolerance selection old-peditaccept)
  (setq ids-str (mcp-json-get-string params "entity_ids_str"))
  (setq tolerance (mcp-json-get-number params "tolerance"))
  (setq selection (mcp-selection-from-handles ids-str))
  (if selection
    (progn
      (if (and tolerance (> tolerance 0))
        (progn
          (setq old-peditaccept (getvar "PEDITACCEPT"))
          (setvar "PEDITACCEPT" 1)
          (command "_.PEDIT" "_M" selection "" "_J" tolerance "")
          (setvar "PEDITACCEPT" old-peditaccept)
        )
        (command "_.JOIN" selection "")
      )
      (cons T (strcat "{\"selected\":" (itoa (sslength selection)) "}")))
    (cons nil "JOIN requires at least one valid entity")
  )
)

(defun mcp-cmd-entity-constrain (params / constraint ids-str selection option)
  (setq constraint (strcase (mcp-json-get-string params "constraint")))
  (setq ids-str (mcp-json-get-string params "entity_ids_str"))
  (setq selection (mcp-selection-from-handles ids-str))
  (setq option
    (cond
      ((= constraint "HORIZONTAL") "_Horizontal")
      ((= constraint "VERTICAL") "_Vertical")
      ((= constraint "PARALLEL") "_Parallel")
      ((= constraint "PERPENDICULAR") "_Perpendicular")
      ((= constraint "TANGENT") "_Tangent")
      ((= constraint "CONCENTRIC") "_Concentric")
      ((= constraint "EQUAL") "_Equal")
      ((= constraint "COLLINEAR") "_Collinear")
      (T nil)
    )
  )
  (if (and selection option)
    (progn
      (command "_.GEOMCONSTRAINT" option selection "")
      (cons T (strcat "{\"constraint\":\"" (mcp-escape-string constraint) "\"}")))
    (cons nil "Unsupported constraint or invalid entity selection")
  )
)

;; --- Layer operations ---

(defun mcp-cmd-layer-set-properties (params / name color linetype lineweight)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (setq lineweight (mcp-json-get-string params "lineweight"))
  (if (not (tblsearch "LAYER" name))
    (cons nil (strcat "Layer not found: " name))
    (progn
      (mcp-update-layer-properties name color linetype lineweight)
      (cons T (strcat "{\"name\":\"" (mcp-escape-string name) "\"}"))
    )
  )
)

(defun mcp-cmd-layer-freeze (params / name)
  (setq name (mcp-json-get-string params "name"))
  (mcp-set-layer-flag name 1 T)
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":true}"))
)

(defun mcp-cmd-layer-thaw (params / name)
  (setq name (mcp-json-get-string params "name"))
  (mcp-set-layer-flag name 1 nil)
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":false}"))
)

(defun mcp-cmd-layer-lock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (mcp-set-layer-flag name 4 T)
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":true}"))
)

(defun mcp-cmd-layer-unlock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (mcp-set-layer-flag name 4 nil)
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":false}"))
)

;; --- Block operations (insert-with-attributes, get-attributes, update-attribute) ---

(defun mcp-cmd-block-insert-with-attribs (params / name x y scale rotation attributes ent)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      ;; Insert with ATTREQ=1 to fill attributes
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      ;; Note: attribute values are applied separately via update-attribute
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}")))
    (cons nil (strcat "Block '" name "' not found"))
  )
)

(defun mcp-cmd-block-get-attributes (params / entity-id ent sub-ent ent-data attribs)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (setq attribs "" sub-ent (entnext ent))
      (while sub-ent
        (setq ent-data (entget sub-ent))
        (if (= (cdr (assoc 0 ent-data)) "ATTRIB")
          (progn
            (if (> (strlen attribs) 0) (setq attribs (strcat attribs ",")))
            (setq attribs (strcat attribs "\"" (cdr (assoc 2 ent-data)) "\":\"" (mcp-escape-string (cdr (assoc 1 ent-data))) "\""))
          )
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
      (cons T (strcat "{\"attributes\":{" attribs "}}"))
    )
  )
)

(defun mcp-cmd-block-update-attribute (params / entity-id tag value ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq value (mcp-json-get-string params "value"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (if c:update-block-attribute
        (progn (c:update-block-attribute ent tag value)
               (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
        ;; Inline fallback if attribute_tools.lsp not loaded
        (progn
          (set_attribute_value ent tag value)
          (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
      )
    )
  )
)

;; --- Annotation commands ---

(defun mcp-cmd-create-dimension-linear (params / x1 y1 x2 y2 dim-x dim-y before-ent created-ent)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq dim-x (mcp-json-get-number params "dim_x"))
  (setq dim-y (mcp-json-get-number params "dim_y"))
  (setq before-ent (entlast))
  (command "_.DIMLINEAR" (list x1 y1 0) (list x2 y2 0) (list dim-x dim-y 0))
  (setq created-ent (entlast))
  (if (or (not created-ent) (eq before-ent created-ent))
    (cons nil "DIMLINEAR completed without creating an entity")
    (cons T (strcat "{\"entity_type\":\"DIMENSION\",\"handle\":\"" (cdr (assoc 5 (entget created-ent))) "\"}"))
  )
)

(defun mcp-cmd-create-dimension-aligned (params / x1 y1 x2 y2 offset before-ent created-ent)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq offset (mcp-json-get-number params "offset"))
  ;; Place dimension line at offset distance
  (setq before-ent (entlast))
  (command "_.DIMALIGNED" (list x1 y1 0) (list x2 y2 0)
    (list (+ (/ (+ x1 x2) 2.0) offset) (+ (/ (+ y1 y2) 2.0) offset) 0))
  (setq created-ent (entlast))
  (if (or (not created-ent) (eq before-ent created-ent))
    (cons nil "DIMALIGNED completed without creating an entity")
    (cons T (strcat "{\"entity_type\":\"DIMENSION\",\"handle\":\"" (cdr (assoc 5 (entget created-ent))) "\"}"))
  )
)

(defun mcp-cmd-create-dimension-angular (params / cx cy x1 y1 x2 y2 before-ent created-ent)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq before-ent (entlast))
  (command "_.DIMANGULAR" (list cx cy 0) (list x1 y1 0) (list x2 y2 0) "")
  (setq created-ent (entlast))
  (if (or (not created-ent) (eq before-ent created-ent))
    (cons nil "DIMANGULAR completed without creating an entity")
    (cons T (strcat "{\"entity_type\":\"DIMENSION\",\"handle\":\"" (cdr (assoc 5 (entget created-ent))) "\"}"))
  )
)

(defun mcp-cmd-create-dimension-radius (params / cx cy radius angle px py before-ent created-ent)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq angle (mcp-json-get-number params "angle"))
  ;; Need a circle/arc entity first, use entity at center
  (setq px (+ cx (* radius (cos (* angle (/ pi 180.0))))))
  (setq py (+ cy (* radius (sin (* angle (/ pi 180.0))))))
  (setq before-ent (entlast))
  (command "_.DIMRADIUS" (list px py 0) "")
  (setq created-ent (entlast))
  (if (or (not created-ent) (eq before-ent created-ent))
    (cons nil "DIMRADIUS completed without creating an entity")
    (cons T (strcat "{\"entity_type\":\"DIMENSION\",\"handle\":\"" (cdr (assoc 5 (entget created-ent))) "\"}"))
  )
)

(defun mcp-cmd-create-leader (params / text pts-str pairs pt-str)
  (setq text (mcp-json-get-string params "text"))
  (setq pts-str (mcp-json-get-string params "points_str"))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_.LEADER")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (command (list (atof (car (mcp-split-string pt-str ",")))
                       (atof (cadr (mcp-split-string pt-str ","))) 0))
      )
      (command "" text "")
      (cons T "{\"entity_type\":\"LEADER\"}")
    )
  )
)

;; --- Drawing management ---

(defun mcp-cmd-drawing-get-variables (params / names-str result var-list var-name var-val first-var)
  (setq names-str (mcp-json-get-string params "names_str"))
  (if (or (not names-str) (= names-str ""))
    ;; Default set when no specific names requested
    (progn
      (setq result "{")
      (setq result (strcat result "\"ACADVER\":\"" (getvar "ACADVER") "\""))
      (setq result (strcat result ",\"DWGNAME\":\"" (mcp-escape-string (getvar "DWGNAME")) "\""))
      (setq result (strcat result ",\"CLAYER\":\"" (getvar "CLAYER") "\""))
      (setq result (strcat result "}"))
      (cons T result)
    )
    ;; Parse semicolon-delimited variable names
    (progn
      (setq var-list (mcp-split-string names-str ";"))
      (setq result "{" first-var T)
      (foreach var-name var-list
        (setq var-val (getvar var-name))
        (if (not first-var) (setq result (strcat result ",")))
        (setq first-var nil)
        (if (not var-val)
          (setq result (strcat result "\"" var-name "\":null"))
          (cond
            ((= (type var-val) 'STR)
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string var-val) "\"")))
            ((= (type var-val) 'INT)
             (setq result (strcat result "\"" var-name "\":" (itoa var-val))))
            ((= (type var-val) 'REAL)
             (setq result (strcat result "\"" var-name "\":" (rtos var-val 2 6))))
            (t
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string (vl-princ-to-string var-val)) "\"")))
          )
        )
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

(defun mcp-cmd-drawing-plot-pdf (params / path)
  (setq path (mcp-json-get-string params "path"))
  (if path
    (progn
      (command "_.-PLOT" "_Y" "" "DWG To PDF.pc3"
        "ANSI_A_(8.50_x_11.00_Inches)" "_Inches" "_Landscape"
        "_N" "_Extents" "_Fit" "_Y" "acad.ctb" "_Y" "_N" "_Y" path "_Y")
      (cons T (strcat "{\"path\":\"" (mcp-escape-string path) "\"}")))
    (cons nil "Plot path required")
  )
)

;; --- P&ID list symbols ---

(defun mcp-cmd-pid-list-symbols (params / category dir-path files result)
  (setq category (mcp-json-get-string params "category"))
  (setq dir-path (strcat "C:/PIDv4-CTO/" category "/"))
  (setq files (vl-directory-files dir-path "*.dwg" 1))
  (setq result "")
  (if files
    (foreach f files
      (if (> (strlen result) 0) (setq result (strcat result ",")))
      ;; Remove .dwg extension
      (setq result (strcat result "\"" (substr f 1 (- (strlen f) 4)) "\""))
    )
  )
  (cons T (strcat "{\"category\":\"" category "\",\"symbols\":[" result "],\"count\":" (itoa (length (if files files '()))) "}"))
)

;; --- Block operations ---

(defun mcp-cmd-block-list ( / blk block-list)
  (setq block-list "" blk (tblnext "BLOCK" T))
  (while blk
    (if (not (= (substr (cdr (assoc 2 blk)) 1 1) "*"))
      (progn
        (if (> (strlen block-list) 0)
          (setq block-list (strcat block-list ",\"" (cdr (assoc 2 blk)) "\""))
          (setq block-list (strcat "\"" (cdr (assoc 2 blk)) "\""))
        )
      )
    )
    (setq blk (tblnext "BLOCK"))
  )
  (cons T (strcat "{\"blocks\":[" block-list "]}"))
)

(defun mcp-cmd-block-insert (params / name x y scale rotation block-id)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq block-id (mcp-json-get-string params "block_id"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      (if (and block-id (> (strlen block-id) 0))
        (set_attribute_value (entlast) "ID" block-id)
      )
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil (strcat "Block '" name "' not found"))
  )
)

;; -----------------------------------------------------------------------
;; ActiveX (Visual LISP COM) helpers
;; Every vla-* call below is wrapped in vl-catch-all-apply so a COM-less
;; session (or an unsupported entity type) yields an honest JSON error.
;; -----------------------------------------------------------------------

(defun mcp-active-document ( / result)
  "Return the ActiveDocument COM object, or nil when ActiveX is unavailable."
  (vl-load-com)
  (setq result (vl-catch-all-apply
    '(lambda () (vla-get-ActiveDocument (vlax-get-acad-object)))
    nil
  ))
  (if (vl-catch-all-error-p result) nil result)
)

(defun mcp-safe-string (value / )
  "Normalize a vl-catch-all-apply result into a plain string."
  (cond
    ((null value) "")
    ((vl-catch-all-error-p value) "")
    ((= (type value) 'STR) value)
    (t (vl-princ-to-string value))
  )
)

(defun mcp-safe-real (value default)
  "Normalize a vl-catch-all-apply result into a real number."
  (cond
    ((null value) default)
    ((vl-catch-all-error-p value) default)
    ((= (type value) 'REAL) value)
    ((= (type value) 'INT) (float value))
    ((= (type value) 'STR) (atof value))
    (t default)
  )
)

(defun mcp-object-type-name (obj / object-name short)
  "Map an ActiveX ObjectName to the DXF-style entity type names used by ezdxf."
  (setq object-name (mcp-safe-string (vl-catch-all-apply 'vla-get-ObjectName (list obj))))
  (setq short
    (if (and (> (strlen object-name) 4) (= (substr object-name 1 4) "AcDb"))
      (substr object-name 5)
      object-name
    )
  )
  (cond
    ((= short "Polyline") "LWPOLYLINE")
    ((= short "2dPolyline") "POLYLINE")
    ((= short "3dPolyline") "POLYLINE3D")
    ((= short "BlockReference") "INSERT")
    ((= short "MText") "MTEXT")
    (t (strcase short))
  )
)

(defun mcp-object-bbox (obj / minpt maxpt result)
  "Return ((minx miny) (maxx maxy)) or nil when the object has no bounds."
  (setq result (vl-catch-all-apply 'vla-getBoundingBox (list obj 'minpt 'maxpt)))
  (if (vl-catch-all-error-p result)
    nil
    (list
      (list (car (vlax-safearray->list minpt)) (cadr (vlax-safearray->list minpt)))
      (list (car (vlax-safearray->list maxpt)) (cadr (vlax-safearray->list maxpt)))
    )
  )
)

(defun mcp-entity-bbox (ent / obj)
  (if ent
    (progn
      (setq obj (vl-catch-all-apply 'vlax-ename->vla-object (list ent)))
      (if (vl-catch-all-error-p obj) nil (mcp-object-bbox obj))
    )
    nil
  )
)

(defun mcp-bbox-union (acc pair / bmin bmax)
  "acc is (minx miny maxx maxy) or nil; pair is ((minx miny) (maxx maxy))."
  (setq bmin (car pair) bmax (cadr pair))
  (if (null acc)
    (list (car bmin) (cadr bmin) (car bmax) (cadr bmax))
    (list
      (min (car acc) (car bmin))
      (min (cadr acc) (cadr bmin))
      (max (caddr acc) (car bmax))
      (max (cadddr acc) (cadr bmax))
    )
  )
)

(defun mcp-bbox-json (entity-id layer-name count acc)
  (strcat "{\"entity_id\":"
    (if entity-id (strcat "\"" (mcp-escape-string entity-id) "\"") "null")
    ",\"layer\":"
    (if layer-name (strcat "\"" (mcp-escape-string layer-name) "\"") "null")
    ",\"count\":" (itoa count)
    ",\"min\":[" (rtos (car acc) 2 6) "," (rtos (cadr acc) 2 6) "]"
    ",\"max\":[" (rtos (caddr acc) 2 6) "," (rtos (cadddr acc) 2 6) "]"
    ",\"width\":" (rtos (- (caddr acc) (car acc)) 2 6)
    ",\"height\":" (rtos (- (cadddr acc) (cadr acc)) 2 6)
    "}"
  )
)

(defun mcp-alist-inc (alist key / entry)
  "Increment (or add) the counter for key in an (name . count) alist."
  (setq entry (assoc key alist))
  (if entry
    (subst (cons key (1+ (cdr entry))) entry alist)
    (append alist (list (cons key 1)))
  )
)

(defun mcp-alist-json (alist / parts)
  (setq parts "")
  (foreach entry alist
    (if (> (strlen parts) 0) (setq parts (strcat parts ",")))
    (setq parts (strcat parts "\"" (mcp-escape-string (car entry)) "\":" (itoa (cdr entry))))
  )
  (strcat "{" parts "}")
)

(defun mcp-table-object (entity-id / ent obj)
  "Resolve a handle to a COM object (used by native-table commands)."
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    nil
    (progn
      (setq obj (vl-catch-all-apply 'vlax-ename->vla-object (list ent)))
      (if (vl-catch-all-error-p obj) nil obj)
    )
  )
)

;; -----------------------------------------------------------------------
;; Inquiry commands
;; -----------------------------------------------------------------------

(defun mcp-cmd-measure-length (params / entity-id ent end-param dist)
  "Curve length via vlax-curve for any entity with a measurable length."
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (cond
    ((not ent) (cons nil (strcat "Entity not found: " entity-id)))
    (t
     (vl-load-com)
     (setq end-param (vl-catch-all-apply 'vlax-curve-getEndParam (list ent)))
     (cond
       ((vl-catch-all-error-p end-param)
        (cons nil (strcat "Entity has no measurable length: " (vl-catch-all-error-message end-param))))
       (t
        (setq dist (vl-catch-all-apply 'vlax-curve-getDistAtParam (list ent end-param)))
        (if (vl-catch-all-error-p dist)
          (cons nil (strcat "Length measurement failed: " (vl-catch-all-error-message dist)))
          (cons T (strcat "{\"entity_id\":\"" (mcp-escape-string entity-id)
                          "\",\"type\":\"" (cdr (assoc 0 (entget ent)))
                          "\",\"length\":" (rtos dist 2 6) "}")))
       )
     )
    )
  )
)

(defun mcp-cmd-measure-area (params / entity-id ent ent-data etype closed-flags obj area)
  "Native area via vla-get-Area; open polylines are rejected like the ezdxf backend."
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (cond
    ((not ent) (cons nil (strcat "Entity not found: " entity-id)))
    (t
     (setq ent-data (entget ent))
     (setq etype (cdr (assoc 0 ent-data)))
     (setq closed-flags (if (assoc 70 ent-data) (cdr (assoc 70 ent-data)) 0))
     (if (and (or (= etype "LWPOLYLINE") (= etype "POLYLINE"))
              (= 0 (logand 1 closed-flags)))
       (cons nil (strcat etype " is not closed; enclosed area is undefined"))
       (progn
         (vl-load-com)
         (setq obj (vl-catch-all-apply 'vlax-ename->vla-object (list ent)))
         (cond
           ((vl-catch-all-error-p obj)
            (cons nil "ActiveX object unavailable for entity"))
           (t
            (setq area (vl-catch-all-apply 'vla-get-Area (list obj)))
            (if (vl-catch-all-error-p area)
              (cons nil (strcat "Area measurement failed: " (vl-catch-all-error-message area)))
              (cons T (strcat "{\"entity_id\":\"" (mcp-escape-string entity-id)
                              "\",\"type\":\"" etype
                              "\",\"area\":" (rtos area 2 6) "}")))
           )
         )
       )
     )
    )
  )
)

(defun mcp-cmd-bbox (params / entity-id layer-name ent doc mspace obj pair acc count)
  "Bounding box of one entity (handle) or the model-space union for a layer."
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq layer-name (mcp-json-get-string params "layer"))
  (if entity-id
    (progn
      (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
      (cond
        ((not ent) (cons nil (strcat "Entity not found: " entity-id)))
        ((not (setq pair (mcp-entity-bbox ent)))
         (cons nil "Entity has no geometry bounds"))
        (t (cons T (mcp-bbox-json entity-id layer-name 1
          (list (car (car pair)) (cadr (car pair)) (car (cadr pair)) (cadr (cadr pair))))))
      )
    )
    (progn
      (setq doc (mcp-active-document))
      (if (not doc)
        (cons nil "ActiveX document access unavailable")
        (progn
          (setq mspace (vla-get-ModelSpace doc))
          (setq acc nil count 0)
          (vlax-for obj mspace
            (if (and (or (not layer-name)
                         (= (strcase (mcp-safe-string (vl-catch-all-apply 'vla-get-Layer (list obj))))
                            (strcase layer-name)))
                     (setq pair (mcp-object-bbox obj)))
              (progn
                (setq acc (mcp-bbox-union acc pair))
                (setq count (1+ count))
              )
            )
          )
          (if (= count 0)
            (cons nil "No entities to measure")
            (cons T (mcp-bbox-json entity-id layer-name count acc))
          )
        )
      )
    )
  )
)

(defun mcp-cmd-summary ( / doc mspace obj etype elayer pair acc total by-type by-layer layers-table layers-json)
  "Count model-space entities by type and layer plus overall extents."
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq mspace (vla-get-ModelSpace doc))
      (setq total 0 acc nil by-type nil by-layer nil)
      (vlax-for obj mspace
        (setq etype (mcp-object-type-name obj))
        (setq elayer (mcp-safe-string (vl-catch-all-apply 'vla-get-Layer (list obj))))
        (setq by-type (mcp-alist-inc by-type etype))
        (setq by-layer (mcp-alist-inc by-layer elayer))
        (setq total (1+ total))
        (if (setq pair (mcp-object-bbox obj))
          (setq acc (mcp-bbox-union acc pair))
        )
      )
      (setq layers-json "")
      (setq layers-table (tblnext "LAYER" T))
      (while layers-table
        (if (> (strlen layers-json) 0) (setq layers-json (strcat layers-json ",")))
        (setq layers-json (strcat layers-json "\"" (mcp-escape-string (cdr (assoc 2 layers-table))) "\""))
        (setq layers-table (tblnext "LAYER"))
      )
      (cons T (strcat "{\"total\":" (itoa total)
        ",\"by_type\":" (mcp-alist-json by-type)
        ",\"by_layer\":" (mcp-alist-json by-layer)
        ",\"layers\":[" layers-json "]"
        (if acc
          (strcat ",\"extents\":{\"min\":[" (rtos (car acc) 2 6) "," (rtos (cadr acc) 2 6)
                  "],\"max\":[" (rtos (caddr acc) 2 6) "," (rtos (cadddr acc) 2 6) "]}")
          "")
        "}"))
    )
  )
)

;; -----------------------------------------------------------------------
;; Selection / spline / explode
;; -----------------------------------------------------------------------

(defun mcp-cmd-entity-select (params / type-name layer-name window-str limit flt ss total idx ent ent-data
                                           etype handle elayer matches collected truncated window-pair wx1 wy1 wx2 wy2 bbox keep)
  "Read-only ssget \"_X\" scan with optional bounding-box window filter."
  (setq type-name (mcp-json-get-string params "type"))
  (setq layer-name (mcp-json-get-string params "layer"))
  (setq window-str (mcp-json-get-string params "window_str"))
  (setq limit (fix (mcp-json-get-number params "limit")))
  (if (not limit) (setq limit 200))
  (if window-str
    (progn
      (setq window-pair (mcp-split-string window-str ","))
      (setq wx1 (atof (nth 0 window-pair)))
      (setq wy1 (atof (nth 1 window-pair)))
      (setq wx2 (atof (nth 2 window-pair)))
      (setq wy2 (atof (nth 3 window-pair)))
    )
  )
  (setq flt nil)
  (if type-name (setq flt (cons (cons 0 type-name) flt)))
  (if layer-name (setq flt (cons (cons 8 layer-name) flt)))
  (vl-load-com)
  (setq ss (ssget "_X" flt))
  (setq total 0 collected 0 matches "" truncated nil)
  (if ss (setq total (sslength ss)))
  (if ss
    (progn
      (setq idx 0)
      (while (< idx total)
        (setq ent (ssname ss idx))
        (setq idx (1+ idx))
        (setq ent-data (entget ent))
        (setq etype (cdr (assoc 0 ent-data)))
        (setq handle (cdr (assoc 5 ent-data)))
        (setq elayer (cdr (assoc 8 ent-data)))
        (setq keep T)
        (if window-str
          (progn
            (setq bbox (mcp-entity-bbox ent))
            (if bbox
              (setq keep (not (or (< (car (cadr bbox)) wx1)
                                  (> (car (car bbox)) wx2)
                                  (< (cadr (cadr bbox)) wy1)
                                  (> (cadr (car bbox)) wy2))))
              (setq keep nil)
            )
          )
        )
        (if keep
          (progn
            (if (< collected limit)
              (progn
                (if (> collected 0) (setq matches (strcat matches ",")))
                (setq matches (strcat matches "{\"handle\":\"" handle "\",\"type\":\"" etype
                                      "\",\"layer\":\"" (mcp-escape-string elayer) "\"}"))
              )
              (setq truncated T)
            )
            (setq collected (1+ collected))
          )
        )
      )
    )
  )
  (cons T (strcat "{\"entities\":[" matches "]"
    ",\"count\":" (itoa (if (> collected limit) limit collected))
    ",\"total_matching\":" (itoa total)
    ",\"truncated\":" (if truncated "true" "false")
    ",\"filters\":{\"type\":"
    (if type-name (strcat "\"" (mcp-escape-string type-name) "\"") "null")
    ",\"layer\":"
    (if layer-name (strcat "\"" (mcp-escape-string layer-name) "\"") "null")
    ",\"window\":"
    (if window-str
      (strcat "[" (rtos wx1 2 6) "," (rtos wy1 2 6) "," (rtos wx2 2 6) "," (rtos wy2 2 6) "]")
      "null")
    "}}"))
)

(defun mcp-cmd-create-spline (params / pts-str closed layer pairs n fit-array zero-3d doc mspace result spline degree idx pt-str cx cy)
  "Fit-point spline via vla-AddSpline; tangents are zero and degree is read back."
  (setq pts-str (mcp-json-get-string params "points_str"))
  (setq closed (mcp-json-get-string params "closed"))
  (setq layer (mcp-json-get-string params "layer"))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (setq pairs (mcp-split-string pts-str ";"))
      (setq n (length pairs))
      (if (< n 3)
        (cons nil "spline requires at least three fit points")
        (progn
          (if layer (ensure_layer_exists layer "white" "CONTINUOUS"))
          (setq doc (mcp-active-document))
          (if (not doc)
            (cons nil "ActiveX document access unavailable")
            (progn
              (setq mspace (vla-get-ModelSpace doc))
              (setq fit-array (vlax-make-safearray vlax-vbDouble (cons 0 (- (* 3 n) 1))))
              (setq idx 0)
              (foreach pt-str pairs
                (setq cx (atof (car (mcp-split-string pt-str ","))))
                (setq cy (atof (cadr (mcp-split-string pt-str ","))))
                (vlax-safearray-put-element fit-array idx (float cx))
                (vlax-safearray-put-element fit-array (+ idx 1) (float cy))
                (vlax-safearray-put-element fit-array (+ idx 2) 0.0)
                (setq idx (+ idx 3))
              )
              (setq zero-3d (vlax-make-variant (vlax-make-safearray vlax-vbDouble '(0 . 2))))
              (setq result (vl-catch-all-apply 'vla-AddSpline
                (list mspace (vlax-make-variant fit-array) zero-3d zero-3d)))
              (cond
                ((vl-catch-all-error-p result)
                 (cons nil (strcat "AddSpline failed: " (vl-catch-all-error-message result))))
                (t
                 (setq spline result)
                 (if (= closed "1")
                   (vl-catch-all-apply 'vla-put-Closed (list spline :vlax-true))
                 )
                 (if layer (vl-catch-all-apply 'vla-put-Layer (list spline layer)))
                 (setq degree (vl-catch-all-apply 'vla-get-Degree (list spline)))
                 (if (vl-catch-all-error-p degree) (setq degree 0))
                 (cons T (strcat "{\"entity_type\":\"SPLINE\",\"handle\":\"" (vla-get-Handle spline)
                                 "\",\"degree\":" (itoa degree) "}"))
                )
              )
            )
          )
        )
      )
    )
  )
)

(defun mcp-cmd-entity-explode (params / entity-id ent obj result pieces handles deleted)
  "Explode via vla-Explode and delete the original, matching ezdxf semantics."
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (cond
    ((not ent) (cons nil (strcat "Entity not found: " entity-id)))
    (t
     (vl-load-com)
     (setq obj (vl-catch-all-apply 'vlax-ename->vla-object (list ent)))
     (cond
       ((vl-catch-all-error-p obj) (cons nil "ActiveX object unavailable for entity"))
       (t
        (setq result (vl-catch-all-apply 'vla-Explode (list obj)))
        (cond
          ((vl-catch-all-error-p result)
           (cons nil (strcat "Explode failed: " (vl-catch-all-error-message result))))
          (t
           (setq pieces (vl-catch-all-apply 'vlax-safearray->list (list result)))
           (if (vl-catch-all-error-p pieces) (setq pieces nil))
           (setq handles "")
           (foreach piece pieces
             (if (> (strlen handles) 0) (setq handles (strcat handles ",")))
             (setq handles (strcat handles "\"" (vla-get-Handle piece) "\""))
           )
           ;; ezdxf replaces the original entity after exploding.
           (setq deleted (vl-catch-all-apply 'vla-Delete (list obj)))
           (cond
             ((vl-catch-all-error-p deleted)
              (cons nil (strcat "Explode created copies but deleting the original failed: "
                                (vl-catch-all-error-message deleted))))
             (t
              (cons T (strcat "{\"exploded\":\"" (mcp-escape-string entity-id)
                              "\",\"created\":" (itoa (length pieces))
                              ",\"handles\":[" handles "]}"))
             )
           )
          )
        )
       )
     )
    )
  )
)

;; -----------------------------------------------------------------------
;; Styles (text, dimension, linetype)
;; -----------------------------------------------------------------------

(defun mcp-cmd-textstyle-list ( / doc result)
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq result "")
      (vlax-for style (vla-get-TextStyles doc)
        (if (> (strlen result) 0) (setq result (strcat result ",")))
        (setq result (strcat result "{\"name\":\"" (mcp-escape-string (vla-get-Name style))
          "\",\"font\":\"" (mcp-escape-string (mcp-safe-string (vl-catch-all-apply 'vla-get-FontFile (list style))))
          "\",\"fixed_height\":" (rtos (mcp-safe-real (vl-catch-all-apply 'vla-get-Height (list style)) 0.0) 2 6)
          "}"))
      )
      (cons T (strcat "{\"text_styles\":[" result "]}"))
    )
  )
)

(defun mcp-cmd-textstyle-create (params / name font fixed-height doc styles existing existed style res)
  (setq name (mcp-json-get-string params "name"))
  (setq font (mcp-json-get-string params "font"))
  (setq fixed-height (mcp-json-get-number params "fixed_height"))
  (if (not font) (setq font "arial.ttf"))
  (cond
    ((not name) (cons nil "Text style name required"))
    (t
     (setq doc (mcp-active-document))
     (if (not doc)
       (cons nil "ActiveX document access unavailable")
       (progn
         (setq styles (vla-get-TextStyles doc))
         (setq existing (vl-catch-all-apply 'vla-Item (list styles name)))
         (setq existed (not (vl-catch-all-error-p existing)))
         (if existed (setq style existing) (setq style (vl-catch-all-apply 'vla-Add (list styles name))))
         (cond
           ((or (vl-catch-all-error-p style) (null style))
            (cons nil (strcat "Unable to create text style: " name)))
           (t
            (setq res (vl-catch-all-apply 'vla-put-FontFile (list style font)))
            (cond
              ((vl-catch-all-error-p res)
               (cons nil (strcat "Setting font failed: " (vl-catch-all-error-message res))))
              (t
               (if (and fixed-height (>= fixed-height 0.0))
                 (vl-catch-all-apply 'vla-put-Height (list style fixed-height))
               )
               (cons T (strcat "{\"name\":\"" (mcp-escape-string name)
                 "\",\"font\":\"" (mcp-escape-string font)
                 "\",\"fixed_height\":" (rtos (if fixed-height fixed-height 0.0) 2 6)
                 ",\"existed\":" (if existed "true" "false") "}"))
             )
            )
           )
         )
       )
     )
    )
  )
)

(defun mcp-cmd-textstyle-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (cond
    ((not name) (cons nil "Text style name required"))
    ((not (tblsearch "STYLE" name)) (cons nil (strcat "Text style '" name "' does not exist")))
    (t
     (setvar "TEXTSTYLE" name)
     (cons T (strcat "{\"current_text_style\":\"" (mcp-escape-string name) "\"}"))
    )
  )
)

(defun mcp-dimstyle-numeric-fields ( / )
  "Numeric DIMSTYLE fields exposed by the backend (lowercase in JSON)."
  '("Dimtxt" "Dimasz" "Dimexe" "Dimexo" "Dimgap" "Dimtad" "Dimjust"
    "Dimdec" "Dimlfac" "Dimscale" "Dimclrd" "Dimclre" "Dimclrt"
    "Dimtih" "Dimtoh" "Dimsd1" "Dimsd2" "Dimlwd" "Dimlwe")
)

(defun mcp-dimstyle-property-name (field / cap i ch)
  "dimtxt -> Dimtxt for vlax-put-property."
  (setq cap "" i 1)
  (while (<= i (strlen field))
    (setq ch (substr field i 1))
    (if (= i 1) (setq ch (strcase ch)))
    (setq cap (strcat cap ch))
    (setq i (1+ i))
  )
  cap
)

(defun mcp-cmd-dimstyle-list ( / doc result entry value)
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq result "")
      (vlax-for style (vla-get-DimStyles doc)
        (setq entry (strcat "{\"name\":\"" (mcp-escape-string (vla-get-Name style)) "\""))
        (foreach field (mcp-dimstyle-numeric-fields)
          (setq value (vl-catch-all-apply 'vlax-get-property (list style field)))
          (if (not (vl-catch-all-error-p value))
            (setq entry (strcat entry ",\"" (strcase field T) "\":"
                                (rtos (mcp-safe-real value 0.0) 2 6)))
          )
        )
        (if (> (strlen result) 0) (setq result (strcat result ",")))
        (setq result (strcat result entry "}"))
      )
      (cons T (strcat "{\"dim_styles\":[" result "],\"current\":\""
                      (mcp-escape-string (getvar "DIMSTYLE")) "\"}"))
    )
  )
)

(defun mcp-cmd-dimstyle-create (params / name values-str doc styles existing existed style applied rejected pair field value res)
  (setq name (mcp-json-get-string params "name"))
  (setq values-str (mcp-json-get-string params "values_str"))
  (cond
    ((not name) (cons nil "Dimension style name required"))
    (t
     (setq doc (mcp-active-document))
     (if (not doc)
       (cons nil "ActiveX document access unavailable")
       (progn
         (setq styles (vla-get-DimStyles doc))
         (setq existing (vl-catch-all-apply 'vla-Item (list styles name)))
         (setq existed (not (vl-catch-all-error-p existing)))
         (if existed (setq style existing) (setq style (vl-catch-all-apply 'vla-Add (list styles name))))
         (cond
           ((or (vl-catch-all-error-p style) (null style))
            (cons nil (strcat "Unable to create dimension style: " name)))
           (t
            (setq applied "" rejected "")
            (foreach token (if values-str (mcp-split-string values-str ";") '())
              (setq pair (mcp-split-string token "="))
              (setq field (strcase (car pair) T))
              (setq value (atof (cadr pair)))
              (setq res (vl-catch-all-apply 'vlax-put-property
                (list style (mcp-dimstyle-property-name field) value)))
              (cond
                ((vl-catch-all-error-p res)
                 (if (> (strlen rejected) 0) (setq rejected (strcat rejected ",")))
                 (setq rejected (strcat rejected "\"" (mcp-escape-string field) "\"")))
                (t
                 (if (> (strlen applied) 0) (setq applied (strcat applied ",")))
                 (setq applied (strcat applied "\"" (mcp-escape-string field) "\":" (rtos value 2 6))))
              )
            )
            (cons T (strcat "{\"name\":\"" (mcp-escape-string name)
              "\",\"applied\":{" applied "}"
              ",\"existed\":" (if existed "true" "false")
              (if (> (strlen rejected) 0) (strcat ",\"rejected\":[" rejected "]") "")
              "}"))
           )
         )
       )
     )
    )
  )
)

(defun mcp-cmd-dimstyle-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (cond
    ((not name) (cons nil "Dimension style name required"))
    ((not (tblsearch "DIMSTYLE" name)) (cons nil (strcat "Dimension style '" name "' does not exist")))
    (t
     (setvar "DIMSTYLE" name)
     (cons T (strcat "{\"current_dim_style\":\"" (mcp-escape-string name) "\"}"))
    )
  )
)

(defun mcp-cmd-linetype-list ( / doc result)
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq result "")
      (vlax-for lt (vla-get-Linetypes doc)
        (if (> (strlen result) 0) (setq result (strcat result ",")))
        (setq result (strcat result "{\"name\":\"" (mcp-escape-string (vla-get-Name lt))
          "\",\"description\":\"" (mcp-escape-string (mcp-safe-string (vl-catch-all-apply 'vla-get-Description (list lt)))) "\"}"))
      )
      (cons T (strcat "{\"linetypes\":[" result "]}"))
    )
  )
)

;; -----------------------------------------------------------------------
;; Layouts (paper space)
;; -----------------------------------------------------------------------

(defun mcp-cmd-layout-list ( / doc result name)
  "Paper-space layout names (Model excluded)."
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq result "")
      (vlax-for layout (vla-get-Layouts doc)
        (setq name (vla-get-Name layout))
        (if (/= (strcase name) "MODEL")
          (progn
            (if (> (strlen result) 0) (setq result (strcat result ",")))
            (setq result (strcat result "\"" (mcp-escape-string name) "\""))
          )
        )
      )
      (cons T (strcat "{\"layouts\":[" result "]}"))
    )
  )
)

(defun mcp-cmd-layout-create (params / name doc result)
  (setq name (mcp-json-get-string params "name"))
  (if (not name)
    (cons nil "Layout name required")
    (progn
      (setq doc (mcp-active-document))
      (if (not doc)
        (cons nil "ActiveX document access unavailable")
        (progn
          (setq result (vl-catch-all-apply 'vla-Add (list (vla-get-Layouts doc) name)))
          (if (vl-catch-all-error-p result)
            (cons nil (strcat "Layout creation failed: " (vl-catch-all-error-message result)))
            (cons T (strcat "{\"name\":\"" (mcp-escape-string name) "\",\"created\":true}"))
          )
        )
      )
    )
  )
)

(defun mcp-cmd-layout-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (cond
    ((not name) (cons nil "Layout name required"))
    ((not (tblsearch "LAYOUT" name)) (cons nil (strcat "Layout '" name "' does not exist")))
    (t
     (setvar "CTAB" name)
     (cons T (strcat "{\"current_layout\":\"" (mcp-escape-string name) "\"}"))
    )
  )
)

(defun mcp-cmd-layout-add-viewport (params / layout-name cx cy w h vcx vcy vh layer doc layout-obj paper vp scale)
  "AddPViewport + CustomScale + Display; view_center is reported for parity."
  (setq layout-name (mcp-json-get-string params "layout"))
  (setq cx (mcp-json-get-number params "center_x"))
  (setq cy (mcp-json-get-number params "center_y"))
  (setq w (mcp-json-get-number params "width"))
  (setq h (mcp-json-get-number params "height"))
  (setq vcx (mcp-json-get-number params "view_center_x"))
  (setq vcy (mcp-json-get-number params "view_center_y"))
  (setq vh (mcp-json-get-number params "view_height"))
  (setq layer (mcp-json-get-string params "layer"))
  (cond
    ((not layout-name) (cons nil "Layout name required"))
    ((or (null cx) (null cy)) (cons nil "center is required"))
    ((or (null w) (<= w 0.0) (null h) (<= h 0.0) (null vh) (<= vh 0.0))
     (cons nil "width, height and view_height must be positive"))
    (t
     (setq doc (mcp-active-document))
     (if (not doc)
       (cons nil "ActiveX document access unavailable")
       (progn
         (setq layout-obj (vl-catch-all-apply 'vla-Item (list (vla-get-Layouts doc) layout-name)))
         (cond
           ((vl-catch-all-error-p layout-obj)
            (cons nil (strcat "Layout '" layout-name "' does not exist")))
           (t
            (if layer (ensure_layer_exists layer "white" "CONTINUOUS"))
            (setq paper (vla-get-Block layout-obj))
            (setq vp (vl-catch-all-apply 'vla-AddPViewport (list paper (list cx cy 0.0) w h)))
            (cond
              ((vl-catch-all-error-p vp)
               (cons nil (strcat "Viewport creation failed: " (vl-catch-all-error-message vp))))
              (t
               (setq scale (/ h vh))
               (vl-catch-all-apply 'vla-put-CustomScale (list vp scale))
               (if layer (vl-catch-all-apply 'vla-put-Layer (list vp layer)))
               (vl-catch-all-apply 'vla-Display (list vp :vlax-true))
               (cons T (strcat "{\"layout\":\"" (mcp-escape-string layout-name)
                 "\",\"handle\":\"" (vla-get-Handle vp)
                 "\",\"center\":[" (rtos cx 2 6) "," (rtos cy 2 6) "]"
                 ",\"width\":" (rtos w 2 6)
                 ",\"height\":" (rtos h 2 6)
                 ",\"view_center\":[" (rtos vcx 2 6) "," (rtos vcy 2 6) "]"
                 ",\"view_height\":" (rtos vh 2 6)
                 ",\"scale\":" (rtos scale 2 8) "}"))
              )
            )
           )
         )
       )
     )
    )
  )
)

;; -----------------------------------------------------------------------
;; Native tables
;; -----------------------------------------------------------------------

(defun mcp-cmd-table-create (params / x y rows cols row-h col-w title cells-str layer doc mspace result table
                                         total-rows row-offset row-index col-index row-token)
  "Native ActiveX table; title becomes a real table title row when given."
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq rows (fix (mcp-json-get-number params "rows")))
  (setq cols (fix (mcp-json-get-number params "cols")))
  (setq row-h (mcp-json-get-number params "row_height"))
  (setq col-w (mcp-json-get-number params "col_width"))
  (setq title (mcp-json-get-string params "title"))
  (setq cells-str (mcp-json-get-string params "cells_str"))
  (setq layer (mcp-json-get-string params "layer"))
  (cond
    ((or (null x) (null y)) (cons nil "x and y are required"))
    ((or (null rows) (< rows 1) (null cols) (< cols 1))
     (cons nil "rows and cols must be positive integers"))
    ((or (null row-h) (<= row-h 0.0) (null col-w) (<= col-w 0.0))
     (cons nil "row_height and col_width must be positive"))
    (t
     (setq doc (mcp-active-document))
     (if (not doc)
       (cons nil "ActiveX document access unavailable")
       (progn
         (setq mspace (vla-get-ModelSpace doc))
         ;; AddTable rows include the title row when not suppressed.
         (setq total-rows (if title (+ rows 1) rows))
         (setq table (vl-catch-all-apply 'vla-AddTable
           (list mspace (list x y 0.0) total-rows cols row-h col-w)))
         (cond
           ((vl-catch-all-error-p table)
            (cons nil (strcat "AddTable failed: " (vl-catch-all-error-message table))))
           (t
            (vl-catch-all-apply 'vla-put-HeaderSuppressed (list table :vlax-true))
            (vl-catch-all-apply 'vla-put-TitleSuppressed
              (list table (if title :vlax-false :vlax-true)))
            (if title (vl-catch-all-apply 'vla-SetText (list table 0 0 title)))
            (setq row-offset (if title 1 0))
            (if cells-str
              (progn
                (setq row-index 0)
                (foreach row-token (mcp-split-string cells-str ";")
                  (setq col-index 0)
                  (foreach cell-token (mcp-split-string row-token "|")
                    (vl-catch-all-apply 'vla-SetText
                      (list table (+ row-offset row-index) col-index cell-token))
                    (setq col-index (1+ col-index))
                  )
                  (setq row-index (1+ row-index))
                )
              )
            )
            (if layer (vl-catch-all-apply 'vla-put-Layer (list table layer)))
            (cons T (strcat "{\"representation\":\"native_table\",\"anchor\":\"" (vla-get-Handle table)
              "\",\"handle\":\"" (vla-get-Handle table)
              "\",\"rows\":" (itoa rows)
              ",\"cols\":" (itoa cols)
              ",\"layer\":\"" (mcp-escape-string (if layer layer "0")) "\"}"))
           )
         )
       )
     )
    )
  )
)

(defun mcp-cmd-table-set-cell (params / entity-id row col text table res)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq row (mcp-json-get-number params "row"))
  (setq col (mcp-json-get-number params "col"))
  (setq text (mcp-json-get-string params "text"))
  (cond
    ((or (null row) (null col)) (cons nil "row and col are required"))
    ((not (setq table (mcp-table-object entity-id)))
     (cons nil (strcat "Entity not found: " entity-id)))
    (t
     (setq res (vl-catch-all-apply 'vla-SetText
       (list table (fix row) (fix col) (if text text ""))))
     (if (vl-catch-all-error-p res)
       (cons nil (strcat "SetCell failed: " (vl-catch-all-error-message res)))
       (cons T (strcat "{\"anchor\":\"" (mcp-escape-string entity-id)
         "\",\"row\":" (itoa (fix row))
         ",\"col\":" (itoa (fix col))
         ",\"text\":\"" (mcp-escape-string (if text text "")) "\"}"))
     )
    )
  )
)

(defun mcp-cmd-table-set-col-widths (params / entity-id widths-str table token json idx failed res)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq widths-str (mcp-json-get-string params "widths_str"))
  (if (not widths-str)
    (cons nil "widths_str required")
    (if (not (setq table (mcp-table-object entity-id)))
      (cons nil (strcat "Entity not found: " entity-id))
      (progn
        (setq json "" idx 0 failed nil)
        (foreach token (mcp-split-string widths-str ";")
          (setq res (vl-catch-all-apply 'vla-SetColumnWidth (list table idx (atof token))))
          (if (vl-catch-all-error-p res)
            (setq failed T)
            (progn
              (if (> idx 0) (setq json (strcat json ",")))
              (setq json (strcat json (rtos (atof token) 2 6)))
            )
          )
          (setq idx (1+ idx))
        )
        (if failed
          (cons nil "SetColumnWidth failed (index out of range or entity is not a table)")
          (cons T (strcat "{\"anchor\":\"" (mcp-escape-string entity-id)
            "\",\"representation\":\"native_table\",\"col_widths\":[" json "]}"))
        )
      )
    )
  )
)

(defun mcp-cmd-table-set-row-heights (params / entity-id heights-str table token json idx failed res)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq heights-str (mcp-json-get-string params "heights_str"))
  (if (not heights-str)
    (cons nil "heights_str required")
    (if (not (setq table (mcp-table-object entity-id)))
      (cons nil (strcat "Entity not found: " entity-id))
      (progn
        (setq json "" idx 0 failed nil)
        (foreach token (mcp-split-string heights-str ";")
          (setq res (vl-catch-all-apply 'vla-SetRowHeight (list table idx (atof token))))
          (if (vl-catch-all-error-p res)
            (setq failed T)
            (progn
              (if (> idx 0) (setq json (strcat json ",")))
              (setq json (strcat json (rtos (atof token) 2 6)))
            )
          )
          (setq idx (1+ idx))
        )
        (if failed
          (cons nil "SetRowHeight failed (index out of range or entity is not a table)")
          (cons T (strcat "{\"anchor\":\"" (mcp-escape-string entity-id)
            "\",\"representation\":\"native_table\",\"row_heights\":[" json "]}"))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; External references (command-based; AutoCAD -XREF prompts)
;; -----------------------------------------------------------------------

(defun mcp-cmd-xref-list ( / doc result name path is-xref)
  (setq doc (mcp-active-document))
  (if (not doc)
    (cons nil "ActiveX document access unavailable")
    (progn
      (setq result "")
      (vlax-for blk (vla-get-Blocks doc)
        (setq is-xref (vl-catch-all-apply 'vla-get-IsXRef (list blk)))
        (if (and (not (vl-catch-all-error-p is-xref)) (= is-xref :vlax-true))
          (progn
            (setq name (vla-get-Name blk))
            (setq path (mcp-safe-string (vl-catch-all-apply 'vla-get-Path (list blk))))
            (if (> (strlen result) 0) (setq result (strcat result ",")))
            (setq result (strcat result "{\"name\":\"" (mcp-escape-string name)
              "\",\"path\":\"" (mcp-escape-string path) "\"}"))
          )
        )
      )
      (cons T (strcat "{\"xrefs\":[" result "]}"))
    )
  )
)

(defun mcp-cmd-xref-attach (params / path x y name attach-name old-filedia result block handle)
  (setq path (mcp-json-get-string params "path"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq name (mcp-json-get-string params "name"))
  (if (not x) (setq x 0.0))
  (if (not y) (setq y 0.0))
  (cond
    ((not path) (cons nil "Xref path required"))
    ((not (findfile path)) (cons nil (strcat "External reference file not found: " path)))
    (t
     ;; "blockname=path" attaches the drawing under a different name.
     (setq attach-name (if (and name (> (strlen name) 0)) (strcat name "=" path) path))
     (setq old-filedia (getvar "FILEDIA"))
     (setvar "FILEDIA" 0)
     (setq result (vl-catch-all-apply 'vl-cmdf
       (list "_.-XREF" "_ATTACH" attach-name (list x y 0.0) 1.0 1.0 0.0)))
     (setvar "FILEDIA" old-filedia)
     (setq block (if (and name (> (strlen name) 0)) name (vl-filename-base path)))
     (cond
       ((vl-catch-all-error-p result)
        (cons nil (strcat "XREF attach failed: " (vl-catch-all-error-message result))))
       ((not (tblsearch "BLOCK" block))
        (cons nil (strcat "XREF attach did not create block: " block)))
       (t
        (setq handle (if (entlast) (cdr (assoc 5 (entget (entlast)))) ""))
        (cons T (strcat "{\"name\":\"" (mcp-escape-string block)
          "\",\"path\":\"" (mcp-escape-string path)
          "\",\"insert\":[" (rtos x 2 6) "," (rtos y 2 6) "]"
          (if (> (strlen handle) 0) (strcat ",\"handle\":\"" handle "\"") "")
          ",\"via\":\"command\"}"))
       )
     )
    )
  )
)

(defun mcp-cmd-xref-detach (params / name old-filedia result)
  (setq name (mcp-json-get-string params "name"))
  (cond
    ((not name) (cons nil "Xref name required"))
    ((not (tblsearch "BLOCK" name)) (cons nil (strcat "Xref '" name "' is not attached")))
    (t
     (setq old-filedia (getvar "FILEDIA"))
     (setvar "FILEDIA" 0)
     (setq result (vl-catch-all-apply 'vl-cmdf (list "_.-XREF" "_DETACH" name)))
     (setvar "FILEDIA" old-filedia)
     (cond
       ((vl-catch-all-error-p result)
        (cons nil (strcat "XREF detach failed: " (vl-catch-all-error-message result))))
       ((tblsearch "BLOCK" name)
        (cons nil (strcat "XREF detach did not remove block: " name)))
       (t
        (cons T (strcat "{\"name\":\"" (mcp-escape-string name)
          "\",\"detached\":true,\"via\":\"command\"}"))
       )
     )
    )
  )
)

(defun mcp-cmd-xref-reload (params / name old-filedia result)
  (setq name (mcp-json-get-string params "name"))
  (cond
    ((not name) (cons nil "Xref name required"))
    ((not (tblsearch "BLOCK" name)) (cons nil (strcat "Xref '" name "' is not attached")))
    (t
     (setq old-filedia (getvar "FILEDIA"))
     (setvar "FILEDIA" 0)
     (setq result (vl-catch-all-apply 'vl-cmdf (list "_.-XREF" "_RELOAD" name)))
     (setvar "FILEDIA" old-filedia)
     (if (vl-catch-all-error-p result)
       (cons nil (strcat "XREF reload failed: " (vl-catch-all-error-message result)))
       (cons T (strcat "{\"name\":\"" (mcp-escape-string name)
         "\",\"reloaded\":true,\"via\":\"command\"}"))
     )
    )
  )
)

;; -----------------------------------------------------------------------
;; Main dispatcher — called by "(c:mcp-dispatch)" from Python
;; -----------------------------------------------------------------------

(defun c:mcp-dispatch ( / cmd-files cmd-file json-text request-id cmd-name params-str result result-file)
  "Find pending command file, dispatch, write result."
  ;; Find first pending command file
  (setq cmd-files (vl-directory-files *mcp-ipc-dir* "autocad_mcp_cmd_*.json" 1))
  (if (not cmd-files)
    (progn (princ "\nMCP: No pending commands") (princ))
    (progn
      ;; Process first command
      (setq cmd-file (strcat *mcp-ipc-dir* (car cmd-files)))
      (setq json-text (mcp-read-file-lines cmd-file))

      (if (not json-text)
        (princ "\nMCP: Cannot read command file")
        (progn
          ;; Parse command
          (setq request-id (mcp-json-get-string json-text "request_id"))
          (setq cmd-name (mcp-json-get-string json-text "command"))

          (if (not cmd-name)
            (princ "\nMCP: No command in payload")
            (progn
              (princ (strcat "\nMCP: Dispatching " cmd-name " [" request-id "]"))

              ;; Execute via whitelist dispatcher
              (setq result
                (vl-catch-all-apply
                  'mcp-dispatch-command
                  (list cmd-name json-text)
                )
              )

              ;; Handle error from vl-catch-all-apply
              (if (vl-catch-all-error-p result)
                (setq result (cons nil (vl-catch-all-error-message result)))
              )

              ;; Write result
              (setq result-file (strcat *mcp-ipc-dir* "autocad_mcp_result_" request-id ".json"))
              (if (car result)
                (mcp-write-result result-file request-id T (cdr result) nil)
                (mcp-write-result result-file request-id nil nil (cdr result))
              )

              (princ (strcat "\nMCP: Done " cmd-name))
            )
          )

          ;; Clean up command file
          (vl-file-delete cmd-file)
        )
      )
    )
  )
  (princ)
)

;; -----------------------------------------------------------------------
;; Utility helpers (defined if not already loaded from external files)
;; -----------------------------------------------------------------------

(defun mcp-color-index (value / name)
  (cond
    ((= (type value) 'INT) value)
    ((= (type value) 'REAL) (fix value))
    ((and value (> (atoi value) 0)) (atoi value))
    (t
      (setq name (strcase (if value value "WHITE")))
      (cond
        ((= name "RED") 1)
        ((= name "YELLOW") 2)
        ((= name "GREEN") 3)
        ((= name "CYAN") 4)
        ((= name "BLUE") 5)
        ((= name "MAGENTA") 6)
        (t 7)
      )
    )
  )
)

(defun mcp-lineweight-value (value)
  (if (not value)
    -3
    (if (< (atof value) 0.0)
      (fix (atof value))
      (fix (+ 0.5 (* 100.0 (atof value))))
    )
  )
)

(defun mcp-ensure-linetype (name / upper created)
  "Ensure common drafting linetypes without opening a file prompt."
  (setq upper (strcase (if name name "CONTINUOUS")))
  (if (tblsearch "LTYPE" upper)
    upper
    (progn
      (setq created
        (cond
          ((= upper "CENTER")
            (entmakex (list
              '(0 . "LTYPE") '(100 . "AcDbSymbolTableRecord")
              '(100 . "AcDbLinetypeTableRecord") (cons 2 "CENTER") '(70 . 0)
              '(3 . "Center ____ _ ____ _ ____") '(72 . 65) '(73 . 4) '(40 . 2.0)
              '(49 . 1.25) '(74 . 0) '(49 . -0.25) '(74 . 0)
              '(49 . 0.25) '(74 . 0) '(49 . -0.25) '(74 . 0)
            )))
          ((= upper "HIDDEN")
            (entmakex (list
              '(0 . "LTYPE") '(100 . "AcDbSymbolTableRecord")
              '(100 . "AcDbLinetypeTableRecord") (cons 2 "HIDDEN") '(70 . 0)
              '(3 . "Hidden __ __ __ __ __") '(72 . 65) '(73 . 2) '(40 . 0.75)
              '(49 . 0.5) '(74 . 0) '(49 . -0.25) '(74 . 0)
            )))
          (t nil)
        )
      )
      (if created upper "CONTINUOUS")
    )
  )
)

(defun mcp-put-dxf (data code value)
  (if (assoc code data)
    (subst (cons code value) (assoc code data) data)
    (append data (list (cons code value)))
  )
)

(defun mcp-update-layer-properties (name color linetype lineweight / data actual)
  (setq data (tblsearch "LAYER" name))
  (if data
    (progn
      (if color (setq data (mcp-put-dxf data 62 (mcp-color-index color))))
      (if linetype
        (progn
          (setq actual (mcp-ensure-linetype linetype))
          (setq data (mcp-put-dxf data 6 actual))
        )
      )
      (if lineweight
        (setq data (mcp-put-dxf data 370 (mcp-lineweight-value lineweight)))
      )
      (entmod data)
      (entupd (tblobjname "LAYER" name))
    )
  )
  (if actual actual (if (assoc 6 data) (cdr (assoc 6 data)) "CONTINUOUS"))
)

(defun mcp-upsert-layer (name color linetype lineweight / actual data)
  "Create or update a layer using symbol-table entities only."
  (setq actual (mcp-ensure-linetype linetype))
  (if (tblsearch "LAYER" name)
    (mcp-update-layer-properties name color actual lineweight)
    (progn
      (setq data (list
        '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord")
        '(100 . "AcDbLayerTableRecord") (cons 2 name) '(70 . 0)
        (cons 62 (mcp-color-index color)) (cons 6 actual)
        (cons 370 (mcp-lineweight-value lineweight))
      ))
      (entmakex data)
    )
  )
  actual
)

(defun mcp-set-layer-flag (name bit enabled / data flags)
  (setq data (tblsearch "LAYER" name))
  (if data
    (progn
      (setq flags (if (assoc 70 data) (cdr (assoc 70 data)) 0))
      (if enabled
        (setq flags (logior flags bit))
        (if (= (logand flags bit) bit) (setq flags (- flags bit)))
      )
      (entmod (mcp-put-dxf data 70 flags))
      (entupd (tblobjname "LAYER" name))
      T
    )
    nil
  )
)

(defun ensure_layer_exists (name color linetype)
  "Compatibility wrapper for prompt-free layer creation."
  (mcp-upsert-layer name color linetype nil)
)

(defun mcp-run-hatch-command (ent pattern scale angle)
  (command "_.-HATCH" "_P" pattern (rtos scale 2 6) (rtos angle 2 6) "_S" ent "" "")
)

(if (not set_current_layer)
  (defun set_current_layer (name)
    "Set a layer as current."
    (setvar "CLAYER" name)
  )
)

(if (not set_attribute_value)
  (defun set_attribute_value (ent tag value / sub-ent ent-data)
    "Set an attribute value on a block insert by tag name."
    (setq sub-ent (entnext ent))
    (while sub-ent
      (setq ent-data (entget sub-ent))
      (if (and (= (cdr (assoc 0 ent-data)) "ATTRIB")
               (= (strcase (cdr (assoc 2 ent-data))) (strcase tag)))
        (progn
          (entmod (subst (cons 1 value) (assoc 1 ent-data) ent-data))
          (entupd sub-ent)
          (setq sub-ent nil)  ; stop
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; Startup message
;; -----------------------------------------------------------------------

  (princ "\n=== MCP Dispatch v4.0.0 loaded ===")
(princ "\nIPC directory: ")
(princ *mcp-ipc-dir*)
(princ "\nReady for commands via (c:mcp-dispatch)")
(princ)
