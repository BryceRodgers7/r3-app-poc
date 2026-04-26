# Sports Replay System -- Target Architecture & Refactor Plan

## Overview

This document defines the target production architecture and a phased
refactor plan for evolving the current proof-of-concept into a
production-grade, high-performance multi-feed sports replay system.

------------------------------------------------------------------------

## Core Architectural Principles

1.  **Control Plane vs Data Plane Separation**
    -   Python = orchestration, UI, control logic
    -   GStreamer = all real-time media processing
2.  **Timestamp-First Design**
    -   All media indexed and synchronized via timestamps (PTS), not
        frame counts
3.  **Feed Independence + Shared Timeline**
    -   Each feed runs independently
    -   All feeds align to a shared session clock
4.  **Segment-Based Storage**
    -   Replace per-frame storage with encoded media segments
5.  **Branch Isolation**
    -   Each pipeline branch (preview, recording, replay) must not block
        others

------------------------------------------------------------------------

## Target System Architecture

### High-Level Flow

camera/NDI → GStreamer pipeline → tee\
→ preview branch (low latency, drop frames if needed)\
→ recording branch (reliable, no drops)\
→ replay branch (rolling segments)\
→ optional analysis branch (appsink)

------------------------------------------------------------------------

## Core Components

### 1. FeedRuntime

-   Owns one full GStreamer pipeline
-   No Python frame pushing

### 2. SessionClock

-   Global timeline authority
-   Maps wall-clock → media timestamps

### 3. ReplayStore (Segment-Based)

Stores: - segment_id - feed_id - start_pts / end_pts - file_path -
keyframe index

### 4. PlaybackController

-   Controls playback across multiple feeds
-   Synchronizes playback using SessionClock

### 5. RecordingManager

-   Starts/stops long-form recording
-   Uses segment writers

------------------------------------------------------------------------

## PHASED IMPLEMENTATION PLAN

------------------------------------------------------------------------

## Phase 1 -- Stabilization & Instrumentation

### Goals

-   Preserve current behavior
-   Add observability

### Tasks

-   Add metrics:
    -   FPS per feed
    -   dropped frames
    -   disk write throughput
    -   replay seek latency
-   Add logging around:
    -   pipeline stalls
    -   replay requests

### Output

-   Stable baseline for comparison

------------------------------------------------------------------------

## Phase 2 -- Eliminate Python from Media Path

### Goals

-   Move all hot-path frame handling into GStreamer

### Tasks

-   Replace Python frame loops with:
    -   native GStreamer sources
-   Ensure tee + queue separation per branch

### Output

-   Reduced CPU + latency
-   Improved stability

------------------------------------------------------------------------

## Phase 3 -- Replace Rolling Replay Storage

### Goals

-   Eliminate JPEG frame storage

### Tasks

-   Implement segment-based replay:
    -   short encoded chunks (2--10 seconds)
-   Build in-memory index:
    -   timestamp → segment mapping

### Output

-   Massive disk + CPU savings
-   Faster replay seek

------------------------------------------------------------------------

## Phase 4 -- Introduce Session Timeline Model

### Goals

-   Enable true multi-angle replay

### Tasks

-   Create:
    -   SessionClock
    -   FeedTimeline
-   Align all feeds to shared timestamps
-   Update replay logic to use timestamps

### Output

-   Synchronized multi-feed playback

------------------------------------------------------------------------

## Phase 5 -- Multi-Feed Replay Engine

### Goals

-   Production-grade replay

### Tasks

-   PlaybackController:
    -   controls all feeds simultaneously
-   Implement:
    -   pause / rewind / slow motion
-   Ensure independent decode pipelines

### Output

-   Fully synchronized replay system

------------------------------------------------------------------------

## Phase 6 -- Performance Optimization

### Goals

-   Scale to real-world usage

### Tasks

-   Enable hardware encoding (NVENC / QuickSync)
-   Tune queue sizes:
    -   preview = low latency
    -   recording = reliability
-   Add drop policies for live preview

### Output

-   High performance under load

------------------------------------------------------------------------

## Phase 7 -- Production Hardening

### Goals

-   Reliability + maintainability

### Tasks

-   Crash recovery:
    -   segment recovery
-   Health checks:
    -   pipeline state monitoring
-   Config profiles:
    -   dev / production

### Output

-   Production-ready system

------------------------------------------------------------------------

## Implementation Guidelines for AI Coding Assistant

Use strict instructions:

-   Never rewrite entire system at once
-   Always:
    -   preserve working behavior
    -   modify one component at a time
-   Prefer:
    -   small vertical slices
-   Validate after every change

------------------------------------------------------------------------

## Final Notes

DO NOT: - revert to frame-based storage - push frames through Python in
hot paths

DO: - trust GStreamer for media flow - treat Python as orchestration
layer

------------------------------------------------------------------------

End of Document
