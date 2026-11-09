#pragma once
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <vector>
#define JNIEXPORT
#define JNICALL
using jlong = int64_t;
using jint = int;
using jsize = int;
using jshort = int16_t;
using jclass = void*;
struct FakeShortArray { std::vector<jshort> values; explicit FakeShortArray(int n) : values(n) {} };
struct FakeLongArray { std::vector<jlong> values; explicit FakeLongArray(int n) : values(n) {} };
using jshortArray = FakeShortArray*;
using jlongArray = FakeLongArray*;
struct JNIEnv {
    template<class T> jsize GetArrayLength(T* array) { return static_cast<int>(array->values.size()); }
    void SetShortArrayRegion(jshortArray a, jsize offset, jsize count, const jshort* source) {
        assert(offset >= 0 && count >= 0 && offset + count <= GetArrayLength(a));
        std::copy_n(source, count, a->values.begin() + offset);
    }
    void GetShortArrayRegion(jshortArray a, jsize offset, jsize count, jshort* destination) {
        assert(offset >= 0 && count >= 0 && offset + count <= GetArrayLength(a));
        std::copy_n(a->values.begin() + offset, count, destination);
    }
    void SetLongArrayRegion(jlongArray a, jsize offset, jsize count, const jlong* source) {
        assert(offset >= 0 && count >= 0 && offset + count <= GetArrayLength(a));
        std::copy_n(source, count, a->values.begin() + offset);
    }
    jlongArray NewLongArray(jsize count) { return new FakeLongArray(count); }
    bool ExceptionCheck() { return false; }
};
