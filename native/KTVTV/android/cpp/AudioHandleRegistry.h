#pragma once
#include <cstdint>
#include <limits>
#include <memory>
#include <mutex>
#include <unordered_map>

// JNI handles are monotonically assigned tokens, never pointers to freed memory.
// Each off-RT operation acquires a shared lifetime lease. Removing a token blocks
// future operations while in-flight reads/writes retain the owner until return.
template<class T> class AudioHandleRegistry {
public:
    int64_t add(const std::shared_ptr<T>& owner) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!owner || next_ == std::numeric_limits<int64_t>::max()) return 0;
        const auto id = next_++;
        owners_.emplace(id, owner);
        return id;
    }
    std::shared_ptr<T> acquire(int64_t id) {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = owners_.find(id);
        return found == owners_.end() ? nullptr : found->second;
    }
    std::shared_ptr<T> remove(int64_t id) {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto found = owners_.find(id);
        if (found == owners_.end()) return nullptr;
        auto owner = std::move(found->second);
        owners_.erase(found);
        return owner;
    }
private:
    std::mutex mutex_;
    std::unordered_map<int64_t, std::shared_ptr<T>> owners_;
    int64_t next_ = 1;
};
